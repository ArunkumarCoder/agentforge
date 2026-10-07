"""Live smoke tests against real provider APIs. They cost a fraction of a cent.

Skipped by default. Run them explicitly with:

    make test-live            # or: uv run pytest -m llm -v

Keys are read the normal way (environment variables, then .env). Each test is
skipped when its key is missing. Override the models with
AGENTFORGE_LIVE_ANTHROPIC_MODEL / AGENTFORGE_LIVE_OPENAI_MODEL.

Ollama tests run only when AGENTFORGE_LIVE_OLLAMA_MODEL is set (e.g. llama3.2)
and the Ollama server is running locally.
"""

import os

import pytest

from agentforge.config import load_settings
from agentforge.core import Message
from agentforge.llm import (
    AnthropicProvider,
    LLMProvider,
    LLMRequest,
    OllamaProvider,
    OpenAIProvider,
    StopReason,
    ToolSpec,
)

pytestmark = pytest.mark.llm

WEATHER_TOOL = ToolSpec(
    name="get_weather",
    description="Get the current weather for a city.",
    input_schema={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)


def _anthropic() -> LLMProvider:
    secret = load_settings().anthropic_api_key
    key = secret.get_secret_value() if secret else pytest.skip("ANTHROPIC_API_KEY not set")
    model = os.environ.get("AGENTFORGE_LIVE_ANTHROPIC_MODEL", "claude-haiku-4-5")
    return AnthropicProvider(model=model, api_key=key, timeout_s=60)


def _openai() -> LLMProvider:
    secret = load_settings().openai_api_key
    key = secret.get_secret_value() if secret else pytest.skip("OPENAI_API_KEY not set")
    model = os.environ.get("AGENTFORGE_LIVE_OPENAI_MODEL", "gpt-5-mini")
    return OpenAIProvider(model=model, api_key=key, timeout_s=60)


def _ollama() -> LLMProvider:
    model = os.environ.get("AGENTFORGE_LIVE_OLLAMA_MODEL") or pytest.skip(
        "AGENTFORGE_LIVE_OLLAMA_MODEL not set"
    )
    return OllamaProvider(model=model, base_url=load_settings().llm.ollama_base_url)


PROVIDERS = [
    pytest.param(_anthropic, id="anthropic"),
    pytest.param(_openai, id="openai"),
    pytest.param(_ollama, id="ollama"),
]


@pytest.mark.parametrize("make", PROVIDERS)
async def test_simple_answer(make: object) -> None:
    provider = make()  # type: ignore[operator]
    response = await provider.complete(
        LLMRequest(
            messages=[
                Message.system("Answer with a single word."),
                Message.user("What is the capital of France?"),
            ],
            max_tokens=512,
        )
    )
    assert "paris" in response.text.lower()
    assert response.usage.input_tokens > 0
    assert response.usage.output_tokens > 0
    print(f"\n{provider.name}: {response.usage} latency={response.latency_ms}ms")


@pytest.mark.parametrize("make", PROVIDERS)
async def test_tool_call(make: object) -> None:
    provider = make()  # type: ignore[operator]
    response = await provider.complete(
        LLMRequest(
            messages=[Message.user("What's the weather in Kochi right now? Use the tool.")],
            tools=[WEATHER_TOOL],
            max_tokens=512,
        )
    )
    assert response.stop_reason is StopReason.TOOL_USE
    (call,) = response.message.tool_calls
    assert call.name == "get_weather"
    assert "kochi" in str(call.arguments.get("city", "")).lower()


@pytest.mark.parametrize("make", PROVIDERS)
async def test_full_tool_loop(make: object) -> None:
    """The model calls a real tool, reads the result and answers with it."""
    from datetime import UTC, datetime

    from agentforge.core import RunContext
    from agentforge.core.loop import run_tool_loop
    from agentforge.log import get_logger
    from agentforge.tools import ToolRegistry, tool

    @tool
    def get_weather(city: str) -> str:
        """Get the current weather for a city."""
        return f"It is 31°C and humid in {city}."

    provider = make()  # type: ignore[operator]
    ctx = RunContext(
        run_id="live",
        correlation_id="live",
        agent="live-test",
        started_at=datetime.now(UTC),
        log=get_logger("live"),
    )
    result = await run_tool_loop(
        provider,
        [Message.user("What's the temperature in Kochi? Use the tool, then answer briefly.")],
        ctx,
        tools=ToolRegistry([get_weather]),
        max_steps=4,
        max_tokens=512,
    )
    assert result.tool_calls >= 1
    assert "31" in result.text
    print(f"\n{provider.name}: {result.steps} steps, {result.usage}")
