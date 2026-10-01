"""BaseAgent: the runtime contract every AgentForge agent implements.

A concrete agent declares its name, description, input/output models and
implements ``execute``. BaseAgent takes care of everything else:

- validating input (a model instance or a plain dict),
- binding ``run_id`` / ``correlation_id`` to logs,
- lifecycle hooks (``before_run`` / ``after_run`` / ``on_error``),
- a per-run timeout,
- validating output against ``output_model``,
- timing, usage accounting and a uniform ``RunResult``.

``arun`` never raises for a failed run: the failure is reported in
``RunResult.status`` and ``RunResult.error`` so callers (CLI, API,
orchestrator) handle every outcome the same way.

Example::

    class EchoInput(AgentInput):
        text: str

    class EchoOutput(AgentOutput):
        text: str

    class EchoAgent(BaseAgent[EchoInput, EchoOutput]):
        name = "echo"
        description = "Returns its input."
        input_model = EchoInput
        output_model = EchoOutput

        async def execute(self, data: EchoInput, ctx: RunContext) -> EchoOutput:
            return EchoOutput(text=data.text)

    result = EchoAgent().run({"text": "hi"})
"""

from __future__ import annotations

import asyncio
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog
from pydantic import ValidationError

from agentforge.core.errors import AgentDefinitionError, AgentOutputError
from agentforge.core.schemas import (
    AgentCard,
    AgentInput,
    AgentOutput,
    ErrorInfo,
    ErrorKind,
    RunResult,
    RunStatus,
    Usage,
    utc_now,
)
from agentforge.log import get_logger, run_context

AGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


@dataclass
class RunContext:
    """Per-run state handed to ``execute`` and the hooks."""

    run_id: str
    correlation_id: str
    agent: str
    started_at: datetime
    log: structlog.typing.FilteringBoundLogger
    usage: Usage = field(default_factory=Usage)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_usage(self, usage: Usage) -> None:
        """Record tokens and cost spent during this run (e.g. by an LLM call)."""
        self.usage = self.usage + usage


class BaseAgent[InT: AgentInput, OutT: AgentOutput](ABC):
    """Base class for all agents. See the module docstring for an example."""

    name: str
    description: str
    version: str = "0.1.0"
    tags: tuple[str, ...] = ()
    input_model: type[InT]
    output_model: type[OutT]
    timeout_s: float | None = 120.0

    def __init__(self) -> None:
        self._check_definition()

    # ------------------------------------------------------------------ to implement

    @abstractmethod
    async def execute(self, data: InT, ctx: RunContext) -> OutT | Mapping[str, Any]:
        """Do the agent's work. May return the output model or a dict matching it."""

    # ------------------------------------------------------------------ optional hooks

    async def before_run(self, data: InT, ctx: RunContext) -> None:  # noqa: B027 (optional hook)
        """Called after input validation, before ``execute``."""

    async def after_run(self, result: RunResult[OutT], ctx: RunContext) -> None:  # noqa: B027 (optional hook)
        """Called after a successful run, before the result is returned."""

    async def on_error(self, error: ErrorInfo, ctx: RunContext) -> None:  # noqa: B027 (optional hook)
        """Called when a run fails for any reason."""

    # ------------------------------------------------------------------ public API

    @classmethod
    def card(cls) -> AgentCard:
        """Describe this agent for discovery and routing."""
        return AgentCard(
            name=cls.name,
            version=cls.version,
            description=cls.description,
            tags=list(cls.tags),
            input_schema=cls.input_model.model_json_schema(),
            output_schema=cls.output_model.model_json_schema(),
        )

    async def arun(
        self,
        data: InT | Mapping[str, Any],
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> RunResult[OutT]:
        """Run the agent and return a ``RunResult``. Never raises for a failed run."""
        with run_context(run_id=run_id, correlation_id=correlation_id) as rid:
            bound = structlog.contextvars.get_contextvars()
            ctx = RunContext(
                run_id=rid,
                correlation_id=bound["correlation_id"],
                agent=self.name,
                started_at=utc_now(),
                log=get_logger(f"agent.{self.name}"),
            )
            start = time.perf_counter()
            ctx.log.info("agent.run.started", agent_version=self.version)

            try:
                validated = self._validate_input(data)
            except ValidationError as exc:
                return await self._fail(ctx, start, ErrorKind.INPUT_VALIDATION, exc)

            try:
                async with asyncio.timeout(self.timeout_s):
                    await self.before_run(validated, ctx)
                    raw = await self.execute(validated, ctx)
                output = self._validate_output(raw)
            except TimeoutError as exc:
                return await self._fail(ctx, start, ErrorKind.TIMEOUT, exc)
            except AgentOutputError as exc:
                return await self._fail(ctx, start, ErrorKind.OUTPUT_VALIDATION, exc)
            except Exception as exc:
                return await self._fail(ctx, start, ErrorKind.EXECUTION, exc)

            result = RunResult[OutT](
                run_id=ctx.run_id,
                correlation_id=ctx.correlation_id,
                agent=self.name,
                agent_version=self.version,
                status=RunStatus.SUCCEEDED,
                output=output,
                usage=ctx.usage,
                started_at=ctx.started_at,
                duration_ms=_elapsed_ms(start),
            )
            await self.after_run(result, ctx)
            ctx.log.info(
                "agent.run.succeeded",
                duration_ms=result.duration_ms,
                total_tokens=result.usage.total_tokens,
                cost_usd=result.usage.cost_usd,
            )
            return result

    def run(
        self,
        data: InT | Mapping[str, Any],
        *,
        run_id: str | None = None,
        correlation_id: str | None = None,
    ) -> RunResult[OutT]:
        """Synchronous wrapper around ``arun`` for scripts and the CLI.

        Raises ``RuntimeError`` if called from inside a running event loop;
        use ``await agent.arun(...)`` there instead.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun(data, run_id=run_id, correlation_id=correlation_id))
        raise RuntimeError("run() cannot be used inside an event loop; use 'await arun()'")

    # ------------------------------------------------------------------ internals

    def _validate_input(self, data: InT | Mapping[str, Any]) -> InT:
        if isinstance(data, self.input_model):
            return data
        if isinstance(data, AgentInput):
            data = data.model_dump()
        return self.input_model.model_validate(data)

    def _validate_output(self, raw: OutT | Mapping[str, Any]) -> OutT:
        if isinstance(raw, self.output_model):
            return raw
        try:
            return self.output_model.model_validate(raw)
        except ValidationError as exc:
            raise AgentOutputError(
                f"{self.name} returned output that does not match "
                f"{self.output_model.__name__}: {exc.error_count()} error(s)"
            ) from exc

    async def _fail(
        self, ctx: RunContext, start: float, kind: ErrorKind, exc: BaseException
    ) -> RunResult[OutT]:
        message = str(exc) or type(exc).__name__
        if kind is ErrorKind.TIMEOUT:
            message = f"run exceeded timeout of {self.timeout_s}s"
        error = ErrorInfo(kind=kind, type=type(exc).__name__, message=message)
        ctx.log.error(
            "agent.run.failed",
            error_kind=kind.value,
            error_type=error.type,
            error_message=error.message,
            exc_info=kind is ErrorKind.EXECUTION,
        )
        try:
            await self.on_error(error, ctx)
        except Exception:
            ctx.log.exception("agent.on_error.failed")
        return RunResult[OutT](
            run_id=ctx.run_id,
            correlation_id=ctx.correlation_id,
            agent=self.name,
            agent_version=self.version,
            status=RunStatus.FAILED,
            error=error,
            usage=ctx.usage,
            started_at=ctx.started_at,
            duration_ms=_elapsed_ms(start),
        )

    @classmethod
    def _check_definition(cls) -> None:
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not AGENT_NAME_PATTERN.match(name):
            raise AgentDefinitionError(
                f"{cls.__name__}.name must be kebab-case like 'code-review', got {name!r}"
            )
        if not getattr(cls, "description", "").strip():
            raise AgentDefinitionError(f"{cls.__name__}.description must not be empty")
        for attr, base in (("input_model", AgentInput), ("output_model", AgentOutput)):
            model = getattr(cls, attr, None)
            if not (isinstance(model, type) and issubclass(model, base)):
                raise AgentDefinitionError(
                    f"{cls.__name__}.{attr} must be a subclass of {base.__name__}"
                )
        if cls.timeout_s is not None and cls.timeout_s <= 0:
            raise AgentDefinitionError(f"{cls.__name__}.timeout_s must be positive or None")


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 3)
