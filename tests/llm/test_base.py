"""Tests for the provider-neutral request/response models and LLMProvider."""

import asyncio
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from agentforge.core import Message, Usage
from agentforge.llm import (
    LLMConfigError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMServerError,
    LLMTimeoutError,
    RetryPolicy,
    StopReason,
    ToolSpec,
)
from tests.conftest import LogCapture


class ScriptedProvider(LLMProvider):
    """In-test provider: each call pops the next action (response, error or delay)."""

    name = "scripted"

    def __init__(self, *actions: object, model: str = "m-default", **kwargs: object) -> None:
        super().__init__(model=model, **kwargs)  # type: ignore[arg-type]
        self.actions = list(actions)
        self.models_used: list[str] = []

    async def _complete(self, request: LLMRequest, model: str) -> LLMResponse:
        self.models_used.append(model)
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        if isinstance(action, float):
            await asyncio.sleep(action)
        return LLMResponse(
            message=Message.assistant("done"),
            stop_reason=StopReason.END_TURN,
            usage=Usage(input_tokens=3, output_tokens=1),
            model=model,
            provider=self.name,
        )


def request(**kwargs: object) -> LLMRequest:
    return LLMRequest(messages=[Message.user("hi")], **kwargs)  # type: ignore[arg-type]


class TestModels:
    def test_system_prompt_joins_system_messages(self) -> None:
        req = LLMRequest(
            messages=[Message.system("be brief"), Message.system("be kind"), Message.user("hi")]
        )
        assert req.system_prompt == "be brief\n\nbe kind"

    def test_needs_a_non_system_message(self) -> None:
        with pytest.raises(ValidationError, match="non-system"):
            LLMRequest(messages=[Message.system("only system")])

    def test_needs_messages(self) -> None:
        with pytest.raises(ValidationError):
            LLMRequest(messages=[])

    @pytest.mark.parametrize("name", ["read file", "", "x" * 65, "dots.not.allowed"])
    def test_tool_name_rules(self, name: str) -> None:
        with pytest.raises(ValidationError):
            ToolSpec(name=name, description="d")

    def test_response_helpers(self) -> None:
        response = LLMResponse(
            message=Message.assistant("hello"),
            stop_reason=StopReason.END_TURN,
            model="m",
            provider="p",
        )
        assert response.text == "hello"
        assert response.wants_tools is False


class TestProvider:
    async def test_complete_uses_default_or_override_model(self) -> None:
        provider = ScriptedProvider(None, None)
        first = await provider.complete(request())
        second = await provider.complete(request(model="m-override"))
        assert provider.models_used == ["m-default", "m-override"]
        assert first.model == "m-default"
        assert second.model == "m-override"

    async def test_latency_and_logs(self, logs: LogCapture) -> None:
        response = await ScriptedProvider(None).complete(request())
        assert response.latency_ms >= 0
        done = next(e for e in logs.entries if e["event"] == "llm.response")
        assert done["provider"] == "scripted"
        assert done["model"] == "m-default"
        assert done["input_tokens"] == 3
        assert done["stop_reason"] == "end_turn"

    async def test_retries_retryable_errors(self, make_retry: Callable[..., RetryPolicy]) -> None:
        provider = ScriptedProvider(LLMServerError("500"), None, retry=make_retry(max_retries=1))
        assert (await provider.complete(request())).text == "done"

    async def test_failure_is_logged_and_raised(
        self, make_retry: Callable[..., RetryPolicy], logs: LogCapture
    ) -> None:
        provider = ScriptedProvider(
            LLMServerError("500", status_code=500), retry=make_retry(max_retries=0)
        )
        with pytest.raises(LLMServerError):
            await provider.complete(request())
        failed = logs.entries[-1]
        assert failed["event"] == "llm.failed"
        assert failed["status_code"] == 500

    async def test_hard_timeout_per_attempt(self, make_retry: Callable[..., RetryPolicy]) -> None:
        provider = ScriptedProvider(5.0, 5.0, timeout_s=0.05, retry=make_retry(max_retries=1))
        with pytest.raises(LLMTimeoutError, match=r"0\.05s"):
            await provider.complete(request())
        assert provider.actions == []  # both attempts timed out

    def test_requires_model_and_positive_timeout(self) -> None:
        with pytest.raises(LLMConfigError):
            ScriptedProvider(model="")
        with pytest.raises(ValueError, match="timeout_s"):
            ScriptedProvider(timeout_s=0)
