"""AnthropicProvider tests: real SDK client, fake HTTP transport."""

from collections.abc import Callable
from typing import Any

import anthropic
import httpx2
import pytest

from agentforge.core import Message, ToolCall, ToolResult
from agentforge.llm import (
    AnthropicProvider,
    LLMAuthError,
    LLMBadRequestError,
    LLMConfigError,
    LLMConnectionError,
    LLMRateLimitError,
    LLMRequest,
    LLMServerError,
    LLMTimeoutError,
    RetryPolicy,
    StopReason,
    ToolSpec,
)
from agentforge.llm.anthropic_provider import to_anthropic_messages
from tests.llm.conftest import MockAPI, RecordingSleep


def ok_body(content: list[dict[str, Any]], stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-test",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }


def error_body(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


@pytest.fixture
def provider(api: MockAPI, make_retry: Callable[..., RetryPolicy]) -> AnthropicProvider:
    client = anthropic.AsyncAnthropic(
        api_key="test-key", max_retries=0, http_client=api.http_client()
    )
    return AnthropicProvider(model="claude-test", client=client, retry=make_retry(max_retries=2))


def hello() -> LLMRequest:
    return LLMRequest(messages=[Message.system("be brief"), Message.user("hi")], max_tokens=50)


async def test_text_response(provider: AnthropicProvider, api: MockAPI) -> None:
    api.reply(body=ok_body([{"type": "text", "text": "Hello!"}]))
    response = await provider.complete(hello())

    assert response.text == "Hello!"
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage.input_tokens == 12
    assert response.usage.output_tokens == 7
    assert response.provider == "anthropic"
    assert response.model == "claude-test"
    assert api.last["system"] == "be brief"
    assert api.last["max_tokens"] == 50
    assert api.last["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert "tools" not in api.last


async def test_tool_use_response(provider: AnthropicProvider, api: MockAPI) -> None:
    api.reply(
        body=ok_body(
            [
                {"type": "text", "text": "Let me look."},
                {"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "a.py"}},
            ],
            stop_reason="tool_use",
        )
    )
    tool = ToolSpec(
        name="read_file",
        description="Read a file",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    response = await provider.complete(
        LLMRequest(messages=[Message.user("open a.py")], tools=[tool])
    )

    assert response.stop_reason is StopReason.TOOL_USE
    assert response.wants_tools
    assert response.message.tool_calls == [
        ToolCall(id="tu_1", name="read_file", arguments={"path": "a.py"})
    ]
    assert api.last["tools"] == [
        {"name": "read_file", "description": "Read a file", "input_schema": tool.input_schema}
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("max_tokens", StopReason.MAX_TOKENS),
        ("stop_sequence", StopReason.STOP_SEQUENCE),
        ("refusal", StopReason.REFUSAL),
        ("pause_turn", StopReason.OTHER),
    ],
)
async def test_stop_reasons(
    provider: AnthropicProvider, api: MockAPI, raw: str, expected: StopReason
) -> None:
    api.reply(body=ok_body([{"type": "text", "text": "..."}], stop_reason=raw))
    assert (await provider.complete(hello())).stop_reason is expected


def test_conversation_with_tool_results_is_converted() -> None:
    call_a = ToolCall(id="t1", name="read_file", arguments={"path": "a.py"})
    call_b = ToolCall(id="t2", name="read_file", arguments={"path": "b.py"})
    messages = [
        Message.system("sys"),
        Message.user("compare a.py and b.py"),
        Message.assistant("Reading both.", tool_calls=[call_a, call_b]),
        Message.tool(ToolResult(tool_call_id="t1", name="read_file", content="A")),
        Message.tool(ToolResult(tool_call_id="t2", name="read_file", content="B", is_error=True)),
        Message.user("and summarise"),
    ]
    assert to_anthropic_messages(messages) == [
        {"role": "user", "content": [{"type": "text", "text": "compare a.py and b.py"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Reading both."},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "a.py"}},
                {"type": "tool_use", "id": "t2", "name": "read_file", "input": {"path": "b.py"}},
            ],
        },
        {
            # both tool results and the follow-up text merged into one user turn
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "A"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "B", "is_error": True},
                {"type": "text", "text": "and summarise"},
            ],
        },
    ]


async def test_rate_limit_is_retried_using_retry_after(
    provider: AnthropicProvider, api: MockAPI, sleep: RecordingSleep
) -> None:
    api.reply(429, error_body("rate_limit_error", "slow down"), headers={"retry-after": "2"})
    api.reply(body=ok_body([{"type": "text", "text": "ok"}]))
    response = await provider.complete(hello())
    assert response.text == "ok"
    assert sleep.delays == [2.0]
    assert len(api.requests) == 2


async def test_overloaded_then_gives_up(
    provider: AnthropicProvider, api: MockAPI, sleep: RecordingSleep
) -> None:
    for _ in range(3):
        api.reply(529, error_body("overloaded_error", "busy"))
    with pytest.raises(LLMServerError) as info:
        await provider.complete(hello())
    assert info.value.status_code == 529
    assert len(api.requests) == 3  # 1 + 2 retries
    assert len(sleep.delays) == 2


@pytest.mark.parametrize(
    ("status", "kind", "expected"),
    [
        (401, "authentication_error", LLMAuthError),
        (400, "invalid_request_error", LLMBadRequestError),
    ],
)
async def test_non_retryable_errors(
    provider: AnthropicProvider, api: MockAPI, status: int, kind: str, expected: type[Exception]
) -> None:
    api.reply(status, error_body(kind, "nope"))
    with pytest.raises(expected):
        await provider.complete(hello())
    assert len(api.requests) == 1


async def test_rate_limit_error_after_retries(provider: AnthropicProvider, api: MockAPI) -> None:
    for _ in range(3):
        api.reply(429, error_body("rate_limit_error", "slow down"))
    with pytest.raises(LLMRateLimitError):
        await provider.complete(hello())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx2.ConnectError("refused"), LLMConnectionError),
        (httpx2.ReadTimeout("slow"), LLMTimeoutError),
    ],
)
async def test_network_errors_are_mapped_and_retried(
    provider: AnthropicProvider, api: MockAPI, error: Exception, expected: type[Exception]
) -> None:
    for _ in range(3):
        api.fail(error)
    with pytest.raises(expected):
        await provider.complete(hello())
    assert len(api.requests) == 3


def test_missing_api_key() -> None:
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider(model="claude-test")


def test_builds_its_own_client_from_key() -> None:
    provider = AnthropicProvider(model="claude-test", api_key="k", timeout_s=12)
    assert provider.timeout_s == 12
