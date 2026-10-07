"""Tests for response caching and record/replay."""

from pathlib import Path

import pytest

from agentforge.core import Message, ToolCall, Usage
from agentforge.llm import (
    CachedProvider,
    DiskCache,
    FakeProvider,
    LLMCacheMissError,
    LLMRequest,
    LLMResponse,
    LLMServerError,
    MemoryCache,
    StopReason,
    ToolSpec,
)
from agentforge.llm.cache import cache_key
from tests.conftest import LogCapture


def ask(text: str = "summarise this", **kwargs: object) -> LLMRequest:
    return LLMRequest(messages=[Message.system("sys"), Message.user(text)], **kwargs)  # type: ignore[arg-type]


def answer(text: str = "done") -> LLMResponse:
    return LLMResponse(
        message=Message.assistant(text),
        stop_reason=StopReason.END_TURN,
        usage=Usage(input_tokens=10, output_tokens=2),
        model="fake-model",
        provider="fake",
    )


def count_json(directory: Path) -> int:
    return len(list(directory.rglob("*.json")))


class TestCacheKey:
    def test_stable_for_equal_requests(self) -> None:
        assert cache_key("p", "m", ask()) == cache_key("p", "m", ask())

    @pytest.mark.parametrize(
        "other",
        [
            ("p2", "m", ask()),
            ("p", "m2", ask()),
            ("p", "m", ask("different prompt")),
            ("p", "m", ask(max_tokens=5)),
            ("p", "m", ask(tools=[ToolSpec(name="t", description="d")])),
        ],
    )
    def test_any_change_is_a_new_key(self, other: tuple[str, str, LLMRequest]) -> None:
        assert cache_key("p", "m", ask()) != cache_key(*other)


class TestMemoryCache:
    def test_lru_eviction(self) -> None:
        cache = MemoryCache(max_entries=2)
        cache.set("a", answer("A"))
        cache.set("b", answer("B"))
        assert cache.get("a") is not None  # "a" is now most recently used
        cache.set("c", answer("C"))
        assert cache.get("b") is None
        assert cache.get("a") is not None
        assert len(cache) == 2

    def test_invalid_size(self) -> None:
        with pytest.raises(ValueError, match="max_entries"):
            MemoryCache(max_entries=0)


class TestDiskCache:
    def test_round_trip_as_readable_json(self, tmp_path: Path) -> None:
        cache = DiskCache(tmp_path)
        key = "ab" + "0" * 62
        cache.set(key, answer("stored"))
        path = tmp_path / "ab" / f"{key}.json"
        assert path.is_file()
        assert '"stored"' in path.read_text(encoding="utf-8")
        restored = cache.get(key)
        assert restored is not None
        assert restored.text == "stored"

    def test_missing_key(self, tmp_path: Path) -> None:
        assert DiskCache(tmp_path).get("ff" + "0" * 62) is None

    def test_read_only_never_writes(self, tmp_path: Path) -> None:
        DiskCache(tmp_path, read_only=True).set("ab" + "0" * 62, answer())
        assert list(tmp_path.iterdir()) == []


class TestCachedProvider:
    async def test_second_identical_request_is_a_hit(self, logs: LogCapture) -> None:
        inner = FakeProvider(script=["first answer"])
        llm = CachedProvider(inner, MemoryCache())

        first = await llm.complete(ask())
        second = await llm.complete(ask())

        assert first.text == second.text == "first answer"
        assert first.cached is False
        assert second.cached is True
        assert second.usage == Usage()  # nothing spent on a hit
        assert len(inner.requests) == 1
        assert (llm.hits, llm.misses) == (1, 1)
        assert "llm.cache.hit" in logs.events()

    async def test_different_requests_are_misses(self) -> None:
        llm = CachedProvider(FakeProvider(), MemoryCache())
        assert (await llm.complete(ask("one"))).text == "[fake] one"
        assert (await llm.complete(ask("two"))).text == "[fake] two"
        assert llm.misses == 2

    async def test_tool_use_responses_are_cached(self) -> None:
        call = ToolCall(id="t1", name="read_file", arguments={"path": "a.py"})
        inner = FakeProvider(script=[call])
        llm = CachedProvider(inner, MemoryCache())
        await llm.complete(ask())
        cached = await llm.complete(ask())
        assert cached.message.tool_calls == [call]

    async def test_truncated_answers_are_not_cached(self) -> None:
        cut = answer("cut").model_copy(update={"stop_reason": StopReason.MAX_TOKENS})
        inner = FakeProvider(script=[cut, "complete answer"])
        llm = CachedProvider(inner, MemoryCache())
        await llm.complete(ask())
        assert (await llm.complete(ask())).text == "complete answer"

    async def test_errors_are_not_cached(self) -> None:
        inner = FakeProvider(script=[LLMServerError("boom"), "recovered"])
        llm = CachedProvider(inner, MemoryCache())
        with pytest.raises(LLMServerError):
            await llm.complete(ask())
        assert (await llm.complete(ask())).text == "recovered"

    async def test_model_override_is_part_of_the_key(self) -> None:
        inner = FakeProvider(script=["from default", "from override"])
        llm = CachedProvider(inner, MemoryCache())
        await llm.complete(ask())
        assert (await llm.complete(ask(model="other"))).text == "from override"


class TestRecordAndReplay:
    async def test_record_with_disk_then_replay_offline(self, tmp_path: Path) -> None:
        # 1. Record: a real provider would be used here; FakeProvider stands in.
        recorder = CachedProvider(FakeProvider(script=["recorded answer"]), DiskCache(tmp_path))
        await recorder.complete(ask())
        assert count_json(tmp_path) == 1

        # 2. Replay: the inner provider must never be called.
        never_called = FakeProvider(script=[])
        replayer = CachedProvider(never_called, DiskCache(tmp_path, read_only=True), replay=True)
        response = await replayer.complete(ask())
        assert response.text == "recorded answer"
        assert response.cached is True
        assert never_called.requests == []

    async def test_replay_miss_fails_instead_of_calling_the_api(
        self, tmp_path: Path, logs: LogCapture
    ) -> None:
        inner = FakeProvider(script=["should not be used"])
        replayer = CachedProvider(inner, DiskCache(tmp_path, read_only=True), replay=True)
        with pytest.raises(LLMCacheMissError, match="AGENTFORGE_LLM__CACHE=disk"):
            await replayer.complete(ask())
        assert inner.requests == []
        assert "llm.cache.miss" in logs.events()
