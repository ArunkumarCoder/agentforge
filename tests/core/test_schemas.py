"""Tests for the core data contracts."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agentforge.core import (
    AgentOutput,
    ErrorInfo,
    ErrorKind,
    Message,
    Role,
    RunResult,
    RunStatus,
    ToolCall,
    ToolResult,
    Usage,
)


class Answer(AgentOutput):
    value: int


def _result(**overrides: object) -> RunResult[Answer]:
    fields: dict[str, object] = {
        "run_id": "r1",
        "correlation_id": "c1",
        "agent": "calc",
        "agent_version": "0.1.0",
        "status": RunStatus.SUCCEEDED,
        "output": Answer(value=42),
        "started_at": datetime(2026, 10, 1, tzinfo=UTC),
        "duration_ms": 12.5,
    }
    fields.update(overrides)
    return RunResult[Answer].model_validate(fields)


class TestMessage:
    def test_constructors_set_roles(self) -> None:
        assert Message.system("be brief").role is Role.SYSTEM
        assert Message.user("hi").role is Role.USER
        assert Message.assistant("hello").role is Role.ASSISTANT

    def test_tool_message_from_result(self) -> None:
        result = ToolResult(tool_call_id="call-1", name="read_file", content="data")
        msg = Message.tool(result)
        assert msg.role is Role.TOOL
        assert msg.tool_call_id == "call-1"
        assert msg.content == "data"
        assert msg.is_error is False

    def test_tool_error_is_carried_over(self) -> None:
        result = ToolResult(tool_call_id="c", name="t", content="boom", is_error=True)
        assert Message.tool(result).is_error is True

    def test_assistant_can_carry_tool_calls(self) -> None:
        call = ToolCall(id="call-1", name="read_file", arguments={"path": "a.py"})
        msg = Message.assistant(tool_calls=[call])
        assert msg.tool_calls[0].arguments == {"path": "a.py"}

    @pytest.mark.parametrize(
        "fields",
        [
            {"role": "tool", "content": "x"},  # tool without tool_call_id
            {"role": "user", "content": "x", "tool_call_id": "c1"},  # tool_call_id on user
            {"role": "user", "tool_calls": [{"id": "c1", "name": "t"}]},  # tool_calls on user
            {"role": "wizard", "content": "x"},  # unknown role
            {"role": "user", "content": "x", "extra": 1},  # unknown field
            {"role": "assistant", "content": "x", "is_error": True},  # is_error on non-tool
        ],
    )
    def test_invalid_messages_are_rejected(self, fields: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            Message.model_validate(fields)

    def test_tool_call_requires_name_and_id(self) -> None:
        with pytest.raises(ValidationError):
            ToolCall(id="", name="read_file")


class TestUsage:
    def test_addition_and_total(self) -> None:
        total = Usage(input_tokens=10, output_tokens=5, cost_usd=0.01) + Usage(
            input_tokens=1, output_tokens=2, cost_usd=0.002
        )
        assert total.input_tokens == 11
        assert total.output_tokens == 7
        assert total.total_tokens == 18
        assert total.cost_usd == pytest.approx(0.012)

    def test_negative_values_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Usage(input_tokens=-1)


class TestRunResult:
    def test_succeeded_result_is_ok(self) -> None:
        result = _result()
        assert result.ok
        assert result.output == Answer(value=42)

    def test_json_round_trip_keeps_typed_output(self) -> None:
        result = _result()
        restored = RunResult[Answer].model_validate_json(result.model_dump_json())
        assert restored == result
        assert isinstance(restored.output, Answer)

    def test_failed_result_needs_error(self) -> None:
        with pytest.raises(ValidationError):
            _result(status=RunStatus.FAILED, output=None)

    def test_succeeded_result_needs_output(self) -> None:
        with pytest.raises(ValidationError):
            _result(output=None)

    def test_succeeded_result_cannot_have_error(self) -> None:
        error = ErrorInfo(kind=ErrorKind.EXECUTION, type="X", message="m")
        with pytest.raises(ValidationError):
            _result(error=error)

    def test_failed_result(self) -> None:
        error = ErrorInfo(kind=ErrorKind.TIMEOUT, type="TimeoutError", message="slow")
        result = _result(status=RunStatus.FAILED, output=None, error=error)
        assert not result.ok
        assert result.error == error
