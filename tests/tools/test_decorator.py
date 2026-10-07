"""Tests for the @tool decorator."""

import time
from typing import Annotated, Literal

import pytest
from pydantic import Field

from agentforge.core import ToolCall
from agentforge.tools import FunctionTool, ToolContext, ToolDefinitionError, ToolPermission, tool


@tool
async def word_count(text: Annotated[str, Field(description="Text to count")]) -> int:
    """Count the words in a text.

    Longer explanation that the model does not need to see.
    """
    return len(text.split())


@tool(name="search_notes", permission=ToolPermission.READ, timeout_s=5)
def search(query: str, limit: int = 3, mode: Literal["exact", "fuzzy"] = "fuzzy") -> list[str]:
    """Search the notes."""
    return [f"{mode}:{query}:{i}" for i in range(limit)]


@tool
async def whoami(ctx: ToolContext) -> str:
    """Return the current run id."""
    return ctx.run_id


def test_decorator_builds_a_tool() -> None:
    assert isinstance(word_count, FunctionTool)
    spec = word_count.spec()
    assert spec.name == "word_count"
    assert spec.description == "Count the words in a text."
    assert spec.input_schema["properties"]["text"]["description"] == "Text to count"
    assert spec.input_schema["required"] == ["text"]
    assert spec.input_schema["additionalProperties"] is False


def test_options_defaults_and_literals() -> None:
    spec = search.spec()
    assert spec.name == "search_notes"
    assert search.timeout_s == 5
    assert spec.input_schema["required"] == ["query"]
    assert spec.input_schema["properties"]["limit"]["default"] == 3
    assert spec.input_schema["properties"]["mode"]["enum"] == ["exact", "fuzzy"]


async def test_async_function_runs() -> None:
    result = await word_count.invoke(
        ToolCall(id="c", name="word_count", arguments={"text": "a b c"}), ToolContext()
    )
    assert result.content == "3"


async def test_sync_function_runs_in_a_thread() -> None:
    result = await search.invoke(
        ToolCall(id="c", name="search_notes", arguments={"query": "q", "limit": 2}), ToolContext()
    )
    assert result.content == '["fuzzy:q:0", "fuzzy:q:1"]'


async def test_sync_function_does_not_block_the_event_loop() -> None:
    @tool(timeout_s=0.05)
    def sleepy() -> str:
        """Sleep a bit."""
        time.sleep(0.3)
        return "done"

    result = await sleepy.invoke(ToolCall(id="c", name="sleepy"), ToolContext())
    assert result.is_error  # the timeout fired while the thread slept
    assert "timed out" in result.content


async def test_extra_arguments_are_rejected() -> None:
    result = await word_count.invoke(
        ToolCall(id="c", name="word_count", arguments={"text": "a", "oops": 1}), ToolContext()
    )
    assert result.is_error
    assert "oops: Extra inputs are not permitted" in result.content


async def test_context_is_injected_but_hidden_from_the_model() -> None:
    assert "ctx" not in whoami.spec().input_schema.get("properties", {})
    result = await whoami.invoke(ToolCall(id="c", name="whoami"), ToolContext(run_id="run-42"))
    assert result.content == "run-42"


def test_missing_type_hint_is_rejected() -> None:
    with pytest.raises(ToolDefinitionError, match="needs a type hint"):

        @tool
        def untyped(x) -> str:  # type: ignore[no-untyped-def]
            """Untyped."""
            return str(x)


def test_varargs_are_rejected() -> None:
    with pytest.raises(ToolDefinitionError, match=r"\*args"):

        @tool
        def variadic(*items: str) -> str:
            """Variadic."""
            return ""


def test_missing_docstring_is_rejected() -> None:
    with pytest.raises(ToolDefinitionError, match="description"):

        @tool
        def nodoc(x: int) -> int:
            return x
