"""OpenAIProvider tests: real SDK client, fake HTTP transport."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest

from agentforge.core import Message, ToolCall, ToolResult
from agentforge.llm import (
    LLMAuthError,
    LLMConfigError,
    LLMConnectionError,
    LLMRequest,
    LLMResponseError,
    LLMServerError,
    OpenAIProvider,
    RetryPolicy,
    StopReason,
    ToolSpec,
)
from agentforge.llm.openai_provider import to_openai_message
from tests.llm.conftest import MockAPI, RecordingSleep


def ok_body(
    message: dict[str, Any], finish_reason: str = "stop", usage: bool = True
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "gpt-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "refusal": None, **message},
                "finish_reason": finish_reason,
                "logprobs": None,
            }
        ],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}
    return body


def error_body(message: str) -> dict[str, Any]:
    return {"error": {"message": message, "type": "error", "code": None, "param": None}}


@pytest.fixture
def provider(api: MockAPI, make_retry: Callable[..., RetryPolicy]) -> OpenAIProvider:
    client = openai.AsyncOpenAI(api_key="test-key", max_retries=0, http_client=api.http_client())
    return OpenAIProvider(model="gpt-test", client=client, retry=make_retry(max_retries=2))


def hello() -> LLMRequest:
    return LLMRequest(messages=[Message.system("be brief"), Message.user("hi")], max_tokens=50)


async def test_text_response(provider: OpenAIProvider, api: MockAPI) -> None:
    api.reply(body=ok_body({"content": "Hello!"}))
    response = await provider.complete(hello())

    assert response.text == "Hello!"
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage.input_tokens == 20
    assert response.usage.output_tokens == 4
    assert response.provider == "openai"
    assert api.last["model"] == "gpt-test"
    assert api.last["max_completion_tokens"] == 50
    assert api.last["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]


async def test_tool_call_response(provider: OpenAIProvider, api: MockAPI) -> None:
    api.reply(
        body=ok_body(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            },
            finish_reason="tool_calls",
        )
    )
    tool = ToolSpec(name="read_file", description="Read a file")
    response = await provider.complete(LLMRequest(messages=[Message.user("open")], tools=[tool]))

    assert response.stop_reason is StopReason.TOOL_USE
    assert response.text == ""
    assert response.message.tool_calls == [
        ToolCall(id="call_1", name="read_file", arguments={"path": "a.py"})
    ]
    assert api.last["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"},
            },
        }
    ]


@pytest.mark.parametrize("arguments", ["{not json", "[1, 2]"])
async def test_bad_tool_arguments(provider: OpenAIProvider, api: MockAPI, arguments: str) -> None:
    api.reply(
        body=ok_body(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "t", "arguments": arguments},
                    }
                ],
            },
            finish_reason="tool_calls",
        )
    )
    with pytest.raises(LLMResponseError, match="tool arguments"):
        await provider.complete(hello())


async def test_refusal(provider: OpenAIProvider, api: MockAPI) -> None:
    api.reply(body=ok_body({"content": None, "refusal": "I can't help with that."}))
    response = await provider.complete(hello())
    assert response.stop_reason is StopReason.REFUSAL
    assert response.text == "I can't help with that."


async def test_length_and_missing_usage(provider: OpenAIProvider, api: MockAPI) -> None:
    api.reply(body=ok_body({"content": "cut"}, finish_reason="length", usage=False))
    response = await provider.complete(hello())
    assert response.stop_reason is StopReason.MAX_TOKENS
    assert response.usage.total_tokens == 0


async def test_no_choices(provider: OpenAIProvider, api: MockAPI) -> None:
    body = ok_body({"content": "x"})
    body["choices"] = []
    api.reply(body=body)
    with pytest.raises(LLMResponseError, match="no choices"):
        await provider.complete(hello())


def test_conversation_is_converted() -> None:
    call = ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})
    assert to_openai_message(Message.assistant(tool_calls=[call])) == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "a.py"})},
            }
        ],
    }
    result = ToolResult(tool_call_id="c1", name="read_file", content="A")
    assert to_openai_message(Message.tool(result)) == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "A",
    }


async def test_server_error_retried_then_succeeds(
    provider: OpenAIProvider, api: MockAPI, sleep: RecordingSleep
) -> None:
    api.reply(503, error_body("unavailable"))
    api.reply(body=ok_body({"content": "ok"}))
    assert (await provider.complete(hello())).text == "ok"
    assert len(sleep.delays) == 1


async def test_server_error_gives_up(provider: OpenAIProvider, api: MockAPI) -> None:
    for _ in range(3):
        api.reply(500, error_body("boom"))
    with pytest.raises(LLMServerError):
        await provider.complete(hello())
    assert len(api.requests) == 3


async def test_auth_error_not_retried(provider: OpenAIProvider, api: MockAPI) -> None:
    api.reply(401, error_body("bad key"))
    with pytest.raises(LLMAuthError):
        await provider.complete(hello())
    assert len(api.requests) == 1


async def test_connection_error(provider: OpenAIProvider, api: MockAPI) -> None:
    for _ in range(3):
        api.fail(httpx2.ConnectError("refused"))
    with pytest.raises(LLMConnectionError):
        await provider.complete(hello())


def test_missing_api_key() -> None:
    with pytest.raises(LLMConfigError, match="OPENAI_API_KEY"):
        OpenAIProvider(model="gpt-test")


def test_builds_its_own_client_with_base_url() -> None:
    provider = OpenAIProvider(model="m", api_key="k", base_url="http://localhost:11434/v1")
    assert provider.model == "m"
