"""Input and output contracts for the Database Schema agent."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from agentforge.core.schemas import AgentInput, AgentOutput


class IdStyle(StrEnum):
    UUID = "uuid"
    BIGINT = "bigint"


class DbSchemaInput(AgentInput):
    """The entities and rules of a system, in words, plus a few conventions."""

    requirements: str = Field(
        min_length=10, description="The entities, their attributes and how they relate"
    )
    id_style: IdStyle = Field(default=IdStyle.UUID, description="Type of surrogate primary keys")
    orm_models: bool = Field(default=True, description="Also write SQLAlchemy 2.0 models")
    instructions: str | None = Field(default=None, description="Extra style or scope guidance")


class DbSchemaOutput(AgentOutput):
    summary: str = Field(min_length=10, description="One or two sentences: what the schema covers")
    ddl: str = Field(min_length=20, description="PostgreSQL DDL, parents before children")
    models: str = Field(
        default="", description="SQLAlchemy 2.0 declarative models; empty if not asked for"
    )
    assumptions: list[str] = Field(
        default_factory=list, description="Choices made where the requirements were silent"
    )
    # Filled in by AgentForge from the checked DDL; the model leaves these alone.
    erd: str = Field(default="", description="Mermaid erDiagram generated from the DDL")
    tables: list[str] = Field(default_factory=list, description="Table names in creation order")
    valid: bool = Field(default=False, description="Parsed and passed every check")
    executed: bool | None = Field(
        default=None,
        description="True: a real PostgreSQL accepted the DDL. None: no server was available",
    )
    warnings: list[str] = Field(default_factory=list, description="Non-blocking design findings")
