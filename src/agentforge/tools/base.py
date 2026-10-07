"""BaseTool: a typed, permissioned function the LLM can call.

A tool declares an ``args_model`` (Pydantic). Its JSON Schema is what the
model sees, and the model's arguments are validated against it before the
tool runs. ``invoke`` never raises: every outcome, including bad arguments,
permission denials, timeouts and crashes, becomes a ``ToolResult`` that is
sent back to the model, so it can correct itself or try something else.

Two ways to define a tool:

1. Subclass ``BaseTool`` (best when the tool needs configuration or state)::

       class ReadFileArgs(BaseModel):
           path: str = Field(description="File path relative to the workspace")

       class ReadFile(BaseTool[ReadFileArgs]):
           name = "read_file"
           description = "Read a UTF-8 text file."
           args_model = ReadFileArgs
           permission = ToolPermission.READ

           async def run(self, args: ReadFileArgs, ctx: ToolContext) -> str:
               ...

2. Decorate a function with ``@tool`` (see ``decorator.py``).
"""

from __future__ import annotations

import asyncio
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from agentforge.core.errors import AgentForgeError
from agentforge.core.schemas import ToolCall, ToolResult
from agentforge.llm.base import ToolSpec
from agentforge.log import get_logger

TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
DEFAULT_MAX_OUTPUT_CHARS = 20_000


class ToolPermission(StrEnum):
    """What a tool is able to do. Agents are granted a set of these."""

    READ = "read"  # read local data (files, repo, settings)
    WRITE = "write"  # change local data
    NETWORK = "network"  # talk to other systems (HTTP, APIs)
    EXEC = "exec"  # run programs or code


SAFE_PERMISSIONS = frozenset({ToolPermission.READ})


class ToolError(AgentForgeError):
    """Raise from ``run`` for an expected failure; the message is shown to the model."""


class ToolDefinitionError(AgentForgeError):
    """A tool is declared incorrectly (bad name, missing description or args model)."""


@dataclass
class ToolContext:
    """What a running tool knows about the run it belongs to."""

    run_id: str = "no-run"
    correlation_id: str = "no-run"
    allowed: frozenset[ToolPermission] = SAFE_PERMISSIONS
    log: structlog.typing.FilteringBoundLogger = field(default_factory=lambda: get_logger("tools"))
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool[ArgsT: BaseModel](ABC):
    """Base class for all tools. See the module docstring for an example."""

    name: str
    description: str
    args_model: type[ArgsT]
    permission: ToolPermission = ToolPermission.READ
    timeout_s: float = 30.0
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS

    def __init__(self) -> None:
        self._check_definition()

    @abstractmethod
    async def run(self, args: ArgsT, ctx: ToolContext) -> Any:
        """Do the work. Return a str, a Pydantic model, or anything JSON-serialisable."""

    def spec(self) -> ToolSpec:
        """What the LLM sees: name, description and the JSON Schema of the arguments."""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, input_schema=schema)

    async def invoke(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Validate, check permission, run with a timeout, and wrap the outcome. Never raises."""
        log = ctx.log.bind(tool=self.name, tool_call_id=call.id)

        def fail(message: str, reason: str) -> ToolResult:
            log.warning("tool.failed", reason=reason, error_message=message)
            return ToolResult(tool_call_id=call.id, name=self.name, content=message, is_error=True)

        if self.permission not in ctx.allowed:
            return fail(
                f"Permission denied: '{self.name}' needs '{self.permission}' permission, "
                f"which this agent does not have.",
                "permission",
            )
        try:
            args = self.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            return fail(f"Invalid arguments for '{self.name}': {_describe(exc)}", "arguments")

        log.info("tool.started", permission=self.permission.value)
        try:
            async with asyncio.timeout(self.timeout_s):
                output = await self.run(args, ctx)
        except TimeoutError:
            return fail(f"'{self.name}' timed out after {self.timeout_s}s.", "timeout")
        except ToolError as exc:
            return fail(str(exc), "tool_error")
        except Exception as exc:
            log.exception("tool.crashed")
            return fail(
                f"'{self.name}' failed with an internal error ({type(exc).__name__}).", "crash"
            )

        content = self._truncate(_to_text(output))
        log.info("tool.succeeded", output_chars=len(content))
        return ToolResult(tool_call_id=call.id, name=self.name, content=content)

    def _truncate(self, text: str) -> str:
        if len(text) <= self.max_output_chars:
            return text
        omitted = len(text) - self.max_output_chars
        return text[: self.max_output_chars] + f"\n...[truncated {omitted} characters]"

    def _check_definition(self) -> None:
        cls = type(self)
        name = getattr(self, "name", None)
        if not isinstance(name, str) or not TOOL_NAME_PATTERN.match(name):
            raise ToolDefinitionError(
                f"{cls.__name__}.name must be snake_case like 'read_file', got {name!r}"
            )
        if not getattr(self, "description", "").strip():
            raise ToolDefinitionError(f"{cls.__name__}.description must not be empty")
        model = getattr(self, "args_model", None)
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise ToolDefinitionError(f"{cls.__name__}.args_model must be a Pydantic model")
        if self.timeout_s <= 0 or self.max_output_chars <= 0:
            raise ToolDefinitionError(f"{cls.__name__}: timeout_s and max_output_chars must be > 0")


def _to_text(output: Any) -> str:
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        return output.model_dump_json()
    return json.dumps(output, default=str, ensure_ascii=False)


def _describe(exc: ValidationError) -> str:
    """Short, model-friendly summary such as "path: Field required; limit: ..."."""
    parts = []
    for error in exc.errors()[:5]:
        where = ".".join(str(p) for p in error["loc"]) or "arguments"
        parts.append(f"{where}: {error['msg']}")
    return "; ".join(parts)
