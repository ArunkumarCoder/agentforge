"""Tests for BaseAgent, using small fake agents (no LLM involved)."""

import asyncio
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import Field

from agentforge.core import (
    AgentDefinitionError,
    AgentInput,
    AgentOutput,
    BaseAgent,
    ErrorInfo,
    ErrorKind,
    RunContext,
    RunResult,
    RunStatus,
    Usage,
)
from tests.conftest import LogCapture

# --------------------------------------------------------------------------- fakes


class EchoInput(AgentInput):
    text: str = Field(min_length=1)
    delay_s: float = 0.0
    fail_with: str | None = None
    bad_output: bool = False


class EchoOutput(AgentOutput):
    text: str
    length: int


class EchoAgent(BaseAgent[EchoInput, EchoOutput]):
    """Fake agent whose behaviour is controlled by its input."""

    name = "echo"
    description = "Echoes its input text."
    tags = ("test",)
    input_model = EchoInput
    output_model = EchoOutput
    timeout_s = 0.5

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def before_run(self, data: EchoInput, ctx: RunContext) -> None:
        self.calls.append("before_run")

    async def execute(self, data: EchoInput, ctx: RunContext) -> EchoOutput | Mapping[str, Any]:
        self.calls.append("execute")
        ctx.add_usage(Usage(input_tokens=10, output_tokens=len(data.text), cost_usd=0.001))
        ctx.log.info("echo.working", text=data.text)
        if data.delay_s:
            await asyncio.sleep(data.delay_s)
        if data.fail_with:
            raise RuntimeError(data.fail_with)
        if data.bad_output:
            return {"text": data.text}  # missing 'length'
        return {"text": data.text, "length": len(data.text)}

    async def after_run(self, result: RunResult[EchoOutput], ctx: RunContext) -> None:
        self.calls.append("after_run")

    async def on_error(self, error: ErrorInfo, ctx: RunContext) -> None:
        self.calls.append(f"on_error:{error.kind.value}")


@pytest.fixture
def agent() -> EchoAgent:
    return EchoAgent()


# --------------------------------------------------------------------------- success


async def test_successful_run_returns_typed_output(agent: EchoAgent) -> None:
    result = await agent.arun(EchoInput(text="hello"))
    assert result.status is RunStatus.SUCCEEDED
    assert result.ok
    assert result.output == EchoOutput(text="hello", length=5)
    assert result.error is None
    assert result.agent == "echo"
    assert result.agent_version == "0.1.0"
    assert result.duration_ms >= 0


async def test_accepts_plain_dict_input(agent: EchoAgent) -> None:
    result = await agent.arun({"text": "from a dict"})
    assert result.ok
    assert result.output is not None
    assert result.output.length == 11


async def test_accepts_other_agent_input_with_same_fields(agent: EchoAgent) -> None:
    class OtherInput(AgentInput):
        text: str

    result = await agent.arun(OtherInput(text="handoff"))  # type: ignore[arg-type]
    assert result.ok


async def test_execute_may_return_the_model_itself() -> None:
    class ModelAgent(EchoAgent):
        async def execute(self, data: EchoInput, ctx: RunContext) -> EchoOutput:
            return EchoOutput(text=data.text.upper(), length=len(data.text))

    result = await ModelAgent().arun({"text": "abc"})
    assert result.output == EchoOutput(text="ABC", length=3)


async def test_hooks_run_in_order(agent: EchoAgent) -> None:
    await agent.arun({"text": "x"})
    assert agent.calls == ["before_run", "execute", "after_run"]


async def test_usage_is_recorded(agent: EchoAgent) -> None:
    result = await agent.arun({"text": "abcd"})
    assert result.usage == Usage(input_tokens=10, output_tokens=4, cost_usd=0.001)


def test_sync_run(agent: EchoAgent) -> None:
    result = agent.run({"text": "sync"})
    assert result.ok


async def test_sync_run_inside_event_loop_is_refused(agent: EchoAgent) -> None:
    with pytest.raises(RuntimeError, match="use 'await arun"):
        agent.run({"text": "x"})


# --------------------------------------------------------------------------- failures


async def test_invalid_input_fails_without_executing(agent: EchoAgent) -> None:
    result = await agent.arun({"text": ""})
    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.kind is ErrorKind.INPUT_VALIDATION
    assert result.output is None
    assert agent.calls == ["on_error:input_validation"]


async def test_exception_in_execute_becomes_failed_result(agent: EchoAgent) -> None:
    result = await agent.arun({"text": "x", "fail_with": "disk on fire"})
    assert result.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.kind is ErrorKind.EXECUTION
    assert result.error.type == "RuntimeError"
    assert result.error.message == "disk on fire"
    assert result.usage.input_tokens == 10  # usage before the failure is kept
    assert agent.calls[-1] == "on_error:execution"


async def test_output_not_matching_model_fails(agent: EchoAgent) -> None:
    result = await agent.arun({"text": "x", "bad_output": True})
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "EchoOutput" in result.error.message


async def test_timeout(agent: EchoAgent) -> None:
    result = await agent.arun({"text": "slow", "delay_s": 2})
    assert result.error is not None
    assert result.error.kind is ErrorKind.TIMEOUT
    assert "0.5s" in result.error.message
    assert result.duration_ms < 1500


async def test_failing_on_error_hook_does_not_break_the_run(logs: LogCapture) -> None:
    class BrokenHookAgent(EchoAgent):
        async def on_error(self, error: ErrorInfo, ctx: RunContext) -> None:
            raise ValueError("hook bug")

    result = await BrokenHookAgent().arun({"text": "x", "fail_with": "boom"})
    assert result.error is not None
    assert result.error.message == "boom"
    assert "agent.on_error.failed" in logs.events()


# --------------------------------------------------------------------------- ids & logs


async def test_run_and_correlation_ids(agent: EchoAgent) -> None:
    result = await agent.arun({"text": "x"}, run_id="run-7", correlation_id="job-1")
    assert result.run_id == "run-7"
    assert result.correlation_id == "job-1"


async def test_correlation_id_defaults_to_run_id(agent: EchoAgent) -> None:
    result = await agent.arun({"text": "x"})
    assert result.correlation_id == result.run_id


async def test_every_log_line_carries_run_ids(agent: EchoAgent, logs: LogCapture) -> None:
    result = await agent.arun({"text": "x"}, correlation_id="job-9")
    assert logs.events() == ["agent.run.started", "echo.working", "agent.run.succeeded"]
    for entry in logs.entries:
        assert entry["run_id"] == result.run_id
        assert entry["correlation_id"] == "job-9"
        assert entry["logger"] == "agent.echo"


async def test_failure_is_logged(agent: EchoAgent, logs: LogCapture) -> None:
    await agent.arun({"text": "x", "fail_with": "boom"})
    failed = logs.entries[-1]
    assert failed["event"] == "agent.run.failed"
    assert failed["level"] == "error"
    assert failed["error_kind"] == "execution"
    assert "RuntimeError: boom" in failed["exception"]


async def test_concurrent_runs_keep_their_own_ids(agent: EchoAgent, logs: LogCapture) -> None:
    results = await asyncio.gather(*(agent.arun({"text": f"t{i}"}) for i in range(5)))
    assert len({r.run_id for r in results}) == 5
    by_run: dict[str, set[str]] = {}
    for entry in logs.entries:
        by_run.setdefault(entry["run_id"], set()).add(entry["correlation_id"])
    assert all(ids == {run_id} for run_id, ids in by_run.items())


# --------------------------------------------------------------------------- card & definition


def test_card_describes_agent() -> None:
    card = EchoAgent.card()
    assert card.name == "echo"
    assert card.tags == ["test"]
    assert card.input_schema["properties"]["text"]["type"] == "string"
    assert "length" in card.output_schema["required"]


def _make_agent(**attrs: Any) -> type[BaseAgent[EchoInput, EchoOutput]]:
    base: dict[str, Any] = {
        "name": "valid-name",
        "description": "d",
        "input_model": EchoInput,
        "output_model": EchoOutput,
        "execute": EchoAgent.execute,
    }
    base.update(attrs)
    return type("Dyn", (BaseAgent,), base)


def test_valid_definition() -> None:
    _make_agent()()


@pytest.mark.parametrize(
    "attrs",
    [
        {"name": "Code Review"},
        {"name": "code_review"},
        {"name": ""},
        {"description": "  "},
        {"input_model": dict},
        {"output_model": EchoInput},
        {"timeout_s": 0},
    ],
)
def test_bad_definitions_are_rejected(attrs: dict[str, Any]) -> None:
    with pytest.raises(AgentDefinitionError):
        _make_agent(**attrs)()


def test_missing_execute_cannot_be_instantiated() -> None:
    class NoExecute(BaseAgent[EchoInput, EchoOutput]):
        name = "no-execute"
        description = "d"
        input_model = EchoInput
        output_model = EchoOutput

    with pytest.raises(TypeError):
        NoExecute()  # type: ignore[abstract]
