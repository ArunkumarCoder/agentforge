"""Tests for the tool-calling loop, driven by a scripted FakeProvider."""

from datetime import UTC, datetime
from typing import Any

import pytest

from agentforge.core import (
    AgentInput,
    AgentOutput,
    BaseAgent,
    ErrorKind,
    Message,
    Role,
    RunContext,
    ToolCall,
)
from agentforge.core.loop import ToolLoopLimitError, run_tool_loop
from agentforge.llm import FakeProvider, LLMRequest
from agentforge.log import get_logger
from agentforge.tools import ToolPermission, ToolRegistry, tool
from tests.conftest import LogCapture

NOTES = {"todo": "buy milk", "ideas": "agent that writes agents"}


@tool
def read_note(title: str) -> str:
    """Read a note by title."""
    if title not in NOTES:
        raise KeyError(title)
    return NOTES[title]


@tool
def list_notes() -> list[str]:
    """List all note titles."""
    return sorted(NOTES)


@tool(permission=ToolPermission.WRITE)
def delete_note(title: str) -> str:
    """Delete a note."""
    NOTES.pop(title, None)
    return "deleted"


TOOLS = ToolRegistry([read_note, list_notes, delete_note])


def ctx() -> RunContext:
    return RunContext(
        run_id="run-1",
        correlation_id="corr-1",
        agent="test",
        started_at=datetime.now(UTC),
        log=get_logger("test"),
    )


def call(name: str, call_id: str = "c1", **arguments: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=arguments)


def start(text: str = "What's on my todo list?") -> list[Message]:
    return [Message.system("You manage notes."), Message.user(text)]


async def test_answer_without_tools() -> None:
    llm = FakeProvider(script=["Nothing to do."])
    result = await run_tool_loop(llm, start(), ctx(), tools=TOOLS)
    assert result.text == "Nothing to do."
    assert result.steps == 1
    assert result.tool_calls == 0
    assert llm.requests[0].tools == TOOLS.specs()  # tools were offered


async def test_single_tool_round_trip() -> None:
    llm = FakeProvider(script=[call("read_note", title="todo"), "Your todo: buy milk."])
    run_ctx = ctx()
    result = await run_tool_loop(llm, start(), run_ctx, tools=TOOLS)

    assert result.text == "Your todo: buy milk."
    assert result.steps == 2
    assert result.tool_calls == 1
    roles = [m.role for m in result.messages]
    assert roles == [Role.SYSTEM, Role.USER, Role.ASSISTANT, Role.TOOL, Role.ASSISTANT]
    tool_message = result.messages[3]
    assert tool_message.content == "buy milk"
    assert tool_message.tool_call_id == "c1"
    # the second request contained the tool result
    assert llm.requests[1].messages[-1] == tool_message
    # usage from both LLM calls reached the run context
    assert run_ctx.usage == result.usage
    assert result.usage.total_tokens > 0


async def test_parallel_tool_calls_keep_order() -> None:
    llm = FakeProvider(
        script=[
            [call("read_note", "a", title="todo"), call("read_note", "b", title="ideas")],
            "Both read.",
        ]
    )
    result = await run_tool_loop(llm, start(), ctx(), tools=TOOLS)
    tool_messages = [m for m in result.messages if m.role is Role.TOOL]
    assert [m.tool_call_id for m in tool_messages] == ["a", "b"]
    assert [m.content for m in tool_messages] == ["buy milk", "agent that writes agents"]
    assert result.tool_calls == 2


async def test_model_recovers_from_a_tool_error() -> None:
    def retry_after_error(request: LLMRequest) -> ToolCall:
        last = request.messages[-1]
        assert last.is_error and "internal error (KeyError)" in last.content
        return call("list_notes", "c2")

    llm = FakeProvider(
        script=[
            call("read_note", title="shopping"),
            retry_after_error,
            "There's no 'shopping' note.",
        ]
    )
    result = await run_tool_loop(llm, start(), ctx(), tools=TOOLS)
    assert result.steps == 3
    assert result.messages[-2].content == '["ideas", "todo"]'


async def test_unknown_tool_is_reported_back() -> None:
    llm = FakeProvider(script=[call("send_email", to="x"), "I can't send email."])
    result = await run_tool_loop(llm, start(), ctx(), tools=TOOLS)
    assert "Unknown tool 'send_email'" in result.messages[3].content


async def test_write_tools_are_blocked_unless_allowed() -> None:
    llm = FakeProvider(script=[call("delete_note", title="todo"), "Couldn't delete."])
    result = await run_tool_loop(llm, start(), ctx(), tools=TOOLS)
    assert "Permission denied" in result.messages[3].content
    assert "todo" in NOTES  # nothing was deleted


async def test_max_steps_guard(logs: LogCapture) -> None:
    llm = FakeProvider(script=[call("list_notes", f"c{i}") for i in range(10)])
    with pytest.raises(ToolLoopLimitError, match="after 3 steps"):
        await run_tool_loop(llm, start(), ctx(), tools=TOOLS, max_steps=3)
    assert len(llm.requests) == 3
    assert "loop.limit" in logs.events()


async def test_invalid_max_steps() -> None:
    with pytest.raises(ValueError, match="max_steps"):
        await run_tool_loop(FakeProvider(), start(), ctx(), max_steps=0)


async def test_no_tools_and_options_are_forwarded() -> None:
    llm = FakeProvider(script=["hi"])
    result = await run_tool_loop(llm, start(), ctx(), max_tokens=77, model="other-model")
    assert llm.requests[0].tools == []
    assert llm.requests[0].max_tokens == 77
    assert llm.requests[0].model == "other-model"
    assert result.truncated is False


# --------------------------------------------------------------------------- inside an agent


class NotesQuestion(AgentInput):
    question: str


class NotesAnswer(AgentOutput):
    answer: str
    tool_calls: int


class NotesAgent(BaseAgent[NotesQuestion, NotesAnswer]):
    name = "notes"
    description = "Answers questions about notes using tools."
    input_model = NotesQuestion
    output_model = NotesAnswer

    def __init__(self, llm: FakeProvider, max_steps: int = 5) -> None:
        super().__init__()
        self.llm = llm
        self.max_steps = max_steps

    async def execute(self, data: NotesQuestion, ctx: RunContext) -> NotesAnswer:
        result = await run_tool_loop(
            self.llm, start(data.question), ctx, tools=TOOLS, max_steps=self.max_steps
        )
        return NotesAnswer(answer=result.text, tool_calls=result.tool_calls)


async def test_agent_using_the_loop() -> None:
    llm = FakeProvider(script=[call("list_notes"), "You have 2 notes."])
    result = await NotesAgent(llm).arun({"question": "How many notes?"})
    assert result.ok
    assert result.output == NotesAnswer(answer="You have 2 notes.", tool_calls=1)
    assert result.usage.total_tokens > 0  # loop usage is reported on the run


async def test_agent_reports_loop_limit_as_failed_run() -> None:
    llm = FakeProvider(script=[call("list_notes", f"c{i}") for i in range(5)])
    result = await NotesAgent(llm, max_steps=2).arun({"question": "loop forever"})
    assert not result.ok
    assert result.error is not None
    assert result.error.kind is ErrorKind.EXECUTION
    assert result.error.type == "ToolLoopLimitError"
