"""Input and output contracts for the Bug Fix agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class BugFixInput(AgentInput):
    """An error plus the code it came from: either the files themselves or a repository path."""

    error: str = Field(min_length=1, description="Traceback, error message or failing test output")
    description: str | None = Field(default=None, description="What the user was doing / expected")
    files: dict[str, str] = Field(
        default_factory=dict, description="Relevant source files: path -> content"
    )
    repo_path: str | None = Field(
        default=None, description="Repository to investigate with read-only tools instead of files"
    )

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if bool(self.files) == bool(self.repo_path):
            raise ValueError("provide exactly one of 'files' or 'repo_path'")
        return self


class Location(Contract):
    file: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)


class BugFixOutput(AgentOutput):
    root_cause: str = Field(
        min_length=10, description="One or two sentences: why the error happens"
    )
    explanation: str = Field(min_length=10, description="How the fix addresses the root cause")
    location: Location = Field(description="Where the bug is (not where it surfaced)")
    confidence: Confidence
    patch: str = Field(
        min_length=10,
        description="Unified diff (--- a/path, +++ b/path, @@ hunks) that fixes the bug",
    )
    test_suggestion: str = Field(min_length=10, description="A test that would catch this bug")
    patch_applies: bool = Field(
        default=False, description="Set by AgentForge after checking the patch; leave false"
    )
