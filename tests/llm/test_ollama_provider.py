"""OllamaProvider tests: OpenAI-compatible API on a fake transport."""

from collections.abc import Callable
from typing import Any

import httpx2
import openai
import pytest

from agentforge.core import Message
from agentforge.llm import (
    LLMBadRequestError,
    LLMConnectionError,
    LLMRequest,
    OllamaProvider,
    RetryPolicy,
)
from tests.llm.conftest import MockAPI


def ok_body(content: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "llama3.2",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
    }


@pytest.fixture
def provider(api: MockAPI, make_retry: Callable[..., RetryPolicy]) -> OllamaProvider:
    client = openai.AsyncOpenAI(
        api_key="ollama",
        base_url="http://localhost:11434/v1",
        max_retries=0,
        http_client=api.http_client(),
    )
    return OllamaProvider(model="llama3.2", client=client, retry=make_retry(max_retries=1))


def hello() -> LLMRequest:
    return LLMRequest(messages=[Message.user("hi")])


async def test_local_answer_is_free(provider: OllamaProvider, api: MockAPI) -> None:
    api.reply(body=ok_body("Hello from llama"))
    response = await provider.complete(hello())
    assert response.text == "Hello from llama"
    assert response.provider == "ollama"
    assert response.usage.input_tokens == 30
    assert response.usage.cost_usd == 0
    assert api.last["model"] == "llama3.2"


async def test_not_running_gives_a_helpful_error(provider: OllamaProvider, api: MockAPI) -> None:
    for _ in range(2):
        api.fail(httpx2.ConnectError("refused"))
    with pytest.raises(LLMConnectionError, match="ollama serve"):
        await provider.complete(hello())


async def test_missing_model_gives_a_helpful_error(provider: OllamaProvider, api: MockAPI) -> None:
    api.reply(404, {"error": {"message": "model not found", "type": "api_error"}})
    with pytest.raises(LLMBadRequestError, match=r"ollama pull llama3\.2"):
        await provider.complete(hello())


async def test_other_errors_pass_through(provider: OllamaProvider, api: MockAPI) -> None:
    api.reply(400, {"error": {"message": "bad", "type": "api_error"}})
    with pytest.raises(LLMBadRequestError, match="bad"):
        await provider.complete(hello())


def test_default_url_and_trailing_slash() -> None:
    assert OllamaProvider(model="m").base_url == "http://localhost:11434"
    assert OllamaProvider(model="m", base_url="http://gpu-box:11434/").base_url == (
        "http://gpu-box:11434"
    )
