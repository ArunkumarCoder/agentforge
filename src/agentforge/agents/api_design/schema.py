"""Input and output contracts for the API Design agent."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from agentforge.core.schemas import AgentInput, AgentOutput


class Auth(StrEnum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY = "api_key"


class Pagination(StrEnum):
    NONE = "none"
    OFFSET = "offset"
    CURSOR = "cursor"


class ApiDesignInput(AgentInput):
    """What the API must do, in words, plus the conventions it must follow."""

    requirements: str = Field(
        min_length=10, description="What the API is for: resources, actions, fields, rules"
    )
    title: str | None = Field(default=None, description="API title (the model picks one if empty)")
    api_version: str = Field(default="1.0.0", description="Value for info.version")
    auth: Auth = Field(default=Auth.NONE, description="Authentication every endpoint needs")
    pagination: Pagination = Field(
        default=Pagination.NONE, description="How list endpoints are paginated"
    )
    instructions: str | None = Field(default=None, description="Extra style or scope guidance")


class ApiDesignOutput(AgentOutput):
    summary: str = Field(min_length=10, description="One or two sentences: what the API covers")
    spec: str = Field(min_length=20, description="The complete OpenAPI 3.1 document as YAML")
    assumptions: list[str] = Field(
        default_factory=list, description="Choices made where the requirements were silent"
    )
    # Filled in by AgentForge from the validated spec; the model leaves these alone.
    valid: bool = Field(default=False, description="Spec validated against OpenAPI 3.1")
    operations: list[str] = Field(default_factory=list, description='e.g. "GET /tasks/{id}"')
    schemas: list[str] = Field(default_factory=list, description="components.schemas names")
    warnings: list[str] = Field(default_factory=list, description="Non-blocking lint findings")
