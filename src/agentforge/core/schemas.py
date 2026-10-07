"""Core data contracts shared by every agent, tool and LLM provider.

Everything here is a Pydantic model so it can be validated, serialised to
JSON (for the API, run history and traces) and exported as JSON Schema
(for LLM structured output and the orchestrator's router).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Contract(BaseModel):
    """Base for all AgentForge models: unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- messages


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(Contract):
    """A request from the LLM to run a tool."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class ToolResult(Contract):
    """The outcome of running a tool, sent back to the LLM."""

    tool_call_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    content: str
    is_error: bool = False


class Message(Contract):
    """One message in a conversation with an LLM."""

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False  # tool messages only: the tool failed

    @model_validator(mode="after")
    def _check_role_fields(self) -> Self:
        if self.role is Role.TOOL and not self.tool_call_id:
            raise ValueError("tool messages need a tool_call_id")
        if self.role is not Role.TOOL and (self.tool_call_id or self.is_error):
            raise ValueError("only tool messages may set tool_call_id or is_error")
        if self.tool_calls and self.role is not Role.ASSISTANT:
            raise ValueError("only assistant messages may contain tool_calls")
        return self

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=Role.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=Role.USER, content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: list[ToolCall] | None = None) -> Message:
        return cls(role=Role.ASSISTANT, content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool(cls, result: ToolResult) -> Message:
        return cls(
            role=Role.TOOL,
            content=result.content,
            tool_call_id=result.tool_call_id,
            is_error=result.is_error,
        )


# --------------------------------------------------------------------------- usage


class Usage(Contract):
    """Token and cost accounting. Usages can be added together."""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


# --------------------------------------------------------------------------- agent I/O


class AgentInput(Contract):
    """Base class for every agent's input model."""


class AgentOutput(Contract):
    """Base class for every agent's output model."""


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ErrorKind(StrEnum):
    INPUT_VALIDATION = "input_validation"
    OUTPUT_VALIDATION = "output_validation"
    TIMEOUT = "timeout"
    EXECUTION = "execution"


class ErrorInfo(Contract):
    kind: ErrorKind
    type: str
    message: str


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunResult[OutT: AgentOutput](Contract):
    """What every agent run returns, whether it succeeded or not."""

    run_id: str
    correlation_id: str
    agent: str
    agent_version: str
    status: RunStatus
    output: OutT | None = None
    error: ErrorInfo | None = None
    usage: Usage = Field(default_factory=Usage)
    started_at: datetime
    duration_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def _check_status(self) -> Self:
        if self.status is RunStatus.SUCCEEDED and (self.output is None or self.error):
            raise ValueError("a succeeded run needs an output and no error")
        if self.status is RunStatus.FAILED and self.error is None:
            raise ValueError("a failed run needs an error")
        return self

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.SUCCEEDED


class AgentCard(Contract):
    """Machine-readable description of an agent, used for discovery and routing."""

    name: str
    version: str
    description: str
    tags: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
