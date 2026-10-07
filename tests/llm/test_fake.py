"""Tests for FakeProvider, the offline LLM used throughout the test suite."""

import pytest

from agentforge.core import Message, ToolCall
from agentforge.llm import (
    FakeProvider,
    FakeScriptExhaustedError,
    LLMRateLimitError,
    LLMRequest,
    LLMResponse,
    RetryPolicy,
    StopReason,
)


def ask(text: str = "hello there") -> LLMRequest:
    return LLMRequest(messages=[Message.system("sys"), Message.user(text)])


async def test_echo_mode() -> None:
    llm = FakeProvider()
    response = await llm.complete(ask("what is 2+2?"))
    assert response.text == "[fake] what is 2+2?"
    assert response.stop_reason is StopReason.END_TURN
    assert response.provider == "fake"
    assert response.model == "fake-model"
    assert llm.remaining == 0


async def test_scripted_text_and_tool_calls() -> None:
    call = ToolCall(id="t1", name="read_file", arguments={"path": "a.py"})
    llm = FakeProvider(script=[call, "a.py has one function."])

    first = await llm.complete(ask())
    assert first.stop_reason is StopReason.TOOL_USE
    assert first.message.tool_calls == [call]

    second = await llm.complete(ask())
    assert second.text == "a.py has one function."
    assert llm.remaining == 0
    assert len(llm.requests) == 2


async def test_multiple_tool_calls_in_one_turn() -> None:
    calls = [ToolCall(id=f"t{i}", name="read_file") for i in range(3)]
    response = await FakeProvider(script=[calls]).complete(ask())
    assert len(response.message.tool_calls) == 3


async def test_callable_item_sees_the_request() -> None:
    llm = FakeProvider(script=[lambda req: req.messages[-1].content.upper()])
    assert (await llm.complete(ask("shout"))).text == "SHOUT"


async def test_full_response_item_is_returned_as_is() -> None:
    canned = LLMResponse(
        message=Message.assistant("cut off"),
        stop_reason=StopReason.MAX_TOKENS,
        model="m",
        provider="fake",
    )
    response = await FakeProvider(script=[canned]).complete(ask())
    assert response.stop_reason is StopReason.MAX_TOKENS


async def test_error_items_are_raised_and_can_be_retried() -> None:
    sleeps: list[float] = []

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    llm = FakeProvider(
        script=[LLMRateLimitError("slow", retry_after=1), "ok"],
        retry=RetryPolicy(max_retries=1, sleep=no_sleep),
    )
    assert (await llm.complete(ask())).text == "ok"
    assert sleeps == [1]


async def test_errors_are_not_retried_by_default() -> None:
    llm = FakeProvider(script=[LLMRateLimitError("slow"), "never reached"])
    with pytest.raises(LLMRateLimitError):
        await llm.complete(ask())
    assert llm.remaining == 1


async def test_exhausted_script_fails_loudly() -> None:
    llm = FakeProvider(script=["only one"])
    await llm.complete(ask())
    with pytest.raises(FakeScriptExhaustedError, match="after 1 call"):
        await llm.complete(ask())


async def test_word_count_usage() -> None:
    response = await FakeProvider(script=["three word answer"]).complete(
        ask("four words right here")
    )
    assert response.usage.input_tokens == 5  # "sys" + 4 words
    assert response.usage.output_tokens == 3
