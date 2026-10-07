"""Tests for BaseTool: schema, validation, permissions, errors and output handling."""

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, Field

from agentforge.core import ToolCall
from agentforge.tools import (
    BaseTool,
    ToolContext,
    ToolDefinitionError,
    ToolError,
    ToolPermission,
)
from tests.conftest import LogCapture


class AddArgs(BaseModel):
    a: int = Field(description="First number")
    b: int = Field(default=0, description="Second number")


class Add(BaseTool[AddArgs]):
    name = "add"
    description = "Add two integers."
    args_model = AddArgs

    async def run(self, args: AddArgs, ctx: ToolContext) -> int:
        return args.a + args.b


class Behaviour(BaseModel):
    mode: str = "ok"


class Moody(BaseTool[Behaviour]):
    """Behaves according to ``mode``: ok, tool_error, crash, slow, model, big."""

    name = "moody"
    description = "Test tool with configurable behaviour."
    args_model = Behaviour
    permission = ToolPermission.READ
    timeout_s = 0.05
    max_output_chars = 50

    async def run(self, args: Behaviour, ctx: ToolContext) -> Any:
        match args.mode:
            case "tool_error":
                raise ToolError("file not found: a.py")
            case "crash":
                raise RuntimeError("secret internal detail")
            case "slow":
                await asyncio.sleep(1)
            case "model":
                return AddArgs(a=1, b=2)
            case "big":
                return "x" * 200
            case "dict":
                return {"files": ["a.py", "b.py"]}
        return "fine"


def call(name: str = "add", **arguments: Any) -> ToolCall:
    return ToolCall(id="call-1", name=name, arguments=arguments)


async def test_spec_exposes_json_schema() -> None:
    spec = Add().spec()
    assert spec.name == "add"
    assert spec.description == "Add two integers."
    assert spec.input_schema["properties"]["a"] == {
        "description": "First number",
        "title": "A",
        "type": "integer",
    }
    assert spec.input_schema["required"] == ["a"]
    assert "title" not in spec.input_schema  # top-level title is noise for the model


async def test_successful_call() -> None:
    result = await Add().invoke(call(a=2, b=3), ToolContext())
    assert result.content == "5"
    assert result.is_error is False
    assert result.tool_call_id == "call-1"
    assert result.name == "add"


async def test_invalid_arguments_become_an_error_the_model_can_read() -> None:
    result = await Add().invoke(call(b="not a number"), ToolContext())
    assert result.is_error
    assert "Invalid arguments for 'add'" in result.content
    assert "a: Field required" in result.content
    assert "b: Input should be a valid integer" in result.content


async def test_permission_is_checked_before_running() -> None:
    class Delete(Add):
        name = "delete_everything"
        permission = ToolPermission.WRITE

    result = await Delete().invoke(call("delete_everything", a=1), ToolContext())
    assert result.is_error
    assert "Permission denied" in result.content
    assert "'write'" in result.content

    allowed = ToolContext(allowed=frozenset({ToolPermission.READ, ToolPermission.WRITE}))
    assert (await Delete().invoke(call("delete_everything", a=1), allowed)).content == "1"


async def test_tool_error_message_is_passed_through() -> None:
    result = await Moody().invoke(call("moody", mode="tool_error"), ToolContext())
    assert result.is_error
    assert result.content == "file not found: a.py"


async def test_crash_is_contained_and_details_hidden(logs: LogCapture) -> None:
    result = await Moody().invoke(call("moody", mode="crash"), ToolContext())
    assert result.is_error
    assert "internal error (RuntimeError)" in result.content
    assert "secret internal detail" not in result.content  # not leaked to the model
    crashed = next(e for e in logs.entries if e["event"] == "tool.crashed")
    assert "secret internal detail" in crashed["exception"]  # but logged for us


async def test_timeout() -> None:
    result = await Moody().invoke(call("moody", mode="slow"), ToolContext())
    assert result.is_error
    assert "timed out after 0.05s" in result.content


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("model", '{"a":1,"b":2}'), ("dict", '{"files": ["a.py", "b.py"]}'), ("ok", "fine")],
)
async def test_outputs_are_serialised(mode: str, expected: str) -> None:
    assert (await Moody().invoke(call("moody", mode=mode), ToolContext())).content == expected


async def test_long_output_is_truncated() -> None:
    result = await Moody().invoke(call("moody", mode="big"), ToolContext())
    assert result.content.startswith("x" * 50)
    assert result.content.endswith("[truncated 150 characters]")


async def test_success_and_failure_are_logged(logs: LogCapture) -> None:
    await Add().invoke(call(a=1), ToolContext(run_id="r1"))
    await Add().invoke(call(), ToolContext(run_id="r1"))
    events = [(e["event"], e.get("tool")) for e in logs.entries]
    assert ("tool.succeeded", "add") in events
    assert ("tool.failed", "add") in events


@pytest.mark.parametrize(
    "attrs",
    [
        {"name": "Add"},
        {"name": "add-numbers"},
        {"name": ""},
        {"description": " "},
        {"args_model": dict},
        {"timeout_s": 0},
    ],
)
def test_bad_definitions(attrs: dict[str, Any]) -> None:
    broken = type("Broken", (Add,), attrs)
    with pytest.raises(ToolDefinitionError):
        broken()
