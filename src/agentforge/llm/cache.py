"""Response caching, and record/replay for offline tests.

``CachedProvider`` wraps any provider. A request is identified by a SHA-256
hash of the provider, model and full request (messages, tools, max_tokens),
so any change to the prompt is a cache miss.

Modes (``AGENTFORGE_LLM__CACHE``):

- ``off``    - no caching (default)
- ``memory`` - in-process LRU; good for a long-running API server
- ``disk``   - JSON files in ``cache_dir``; saves money while iterating on an
  agent, and *records* responses you can commit as test fixtures
- ``replay`` - read-only disk cache; a miss raises ``LLMCacheMissError``
  instead of calling the API. Use it in CI to replay recorded responses.

Only successful, complete answers are cached (not errors, not truncated
``max_tokens`` replies). Cache hits are returned with ``cached=True`` and zero
usage, because no tokens were spent.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict
from pathlib import Path
from typing import Protocol

from agentforge.core.schemas import Usage
from agentforge.llm.base import LLMProvider, LLMRequest, LLMResponse, StopReason
from agentforge.llm.errors import LLMError
from agentforge.llm.retry import RetryPolicy

CACHE_FORMAT_VERSION = 1
_CACHEABLE = {StopReason.END_TURN, StopReason.TOOL_USE}


class LLMCacheMissError(LLMError):
    """Replay mode found no recorded response for this request."""


class ResponseCache(Protocol):
    blocking_io: bool  # True if get/set touch the disk (run off the event loop)

    def get(self, key: str) -> LLMResponse | None: ...
    def set(self, key: str, response: LLMResponse) -> None: ...


def cache_key(provider: str, model: str, request: LLMRequest) -> str:
    payload = {
        "v": CACHE_FORMAT_VERSION,
        "provider": provider,
        "model": model,
        "request": request.model_dump(mode="json", exclude={"model"}),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class MemoryCache:
    """In-process LRU cache."""

    blocking_io = False

    def __init__(self, max_entries: int = 1000) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self.max_entries = max_entries
        self._data: OrderedDict[str, LLMResponse] = OrderedDict()

    def get(self, key: str) -> LLMResponse | None:
        response = self._data.get(key)
        if response is not None:
            self._data.move_to_end(key)
        return response

    def set(self, key: str, response: LLMResponse) -> None:
        self._data[key] = response
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def __len__(self) -> int:
        return len(self._data)


class DiskCache:
    """One JSON file per response: ``<dir>/<key[:2]>/<key>.json``. Human-readable and diffable."""

    blocking_io = True

    def __init__(self, directory: Path | str, *, read_only: bool = False) -> None:
        self.directory = Path(directory)
        self.read_only = read_only

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / f"{key}.json"

    def get(self, key: str) -> LLMResponse | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return LLMResponse.model_validate_json(path.read_text(encoding="utf-8"))

    def set(self, key: str, response: LLMResponse) -> None:
        if self.read_only:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(response.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic: readers never see a half-written file


class CachedProvider(LLMProvider):
    """Wraps another provider and serves repeated requests from a cache."""

    name = "cached"

    def __init__(self, inner: LLMProvider, cache: ResponseCache, *, replay: bool = False) -> None:
        super().__init__(
            model=inner.model,
            timeout_s=inner.timeout_s,
            retry=RetryPolicy(max_retries=0),  # the inner provider already retries
            pricing=inner.pricing,
        )
        self.inner = inner
        self.cache = cache
        self.replay = replay
        self.hits = 0
        self.misses = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = request.model or self.inner.model
        key = cache_key(self.inner.name, model, request)
        log = self._log.bind(provider=self.inner.name, model=model, cache_key=key[:12])

        cached = await self._cache_get(key)
        if cached is not None:
            self.hits += 1
            log.info("llm.cache.hit")
            return cached.model_copy(update={"cached": True, "usage": Usage(), "latency_ms": 0.0})

        self.misses += 1
        if self.replay:
            log.error("llm.cache.miss", replay=True)
            raise LLMCacheMissError(
                f"no recorded response for this request (key {key[:12]}). Record it with "
                "AGENTFORGE_LLM__CACHE=disk and a real provider, then commit the file.",
                provider=self.inner.name,
            )
        log.debug("llm.cache.miss")
        response = await self.inner.complete(request)
        if response.stop_reason in _CACHEABLE:
            await self._cache_set(key, response)
        return response

    async def _cache_get(self, key: str) -> LLMResponse | None:
        if self.cache.blocking_io:
            return await asyncio.to_thread(self.cache.get, key)
        return self.cache.get(key)

    async def _cache_set(self, key: str, response: LLMResponse) -> None:
        if self.cache.blocking_io:
            await asyncio.to_thread(self.cache.set, key, response)
        else:
            self.cache.set(key, response)

    async def _complete(self, request: LLMRequest, model: str) -> LLMResponse:
        raise NotImplementedError("CachedProvider delegates in complete()")  # pragma: no cover
