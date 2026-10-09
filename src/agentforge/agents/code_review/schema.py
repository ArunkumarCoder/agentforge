"""Input and output contracts for the Code Review agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract


class Severity(StrEnum):
    CRITICAL = "critical"  # must fix before merge: security hole, data loss, crash in main path
    MAJOR = "major"  # a real bug or risk that should be fixed in this change
    MINOR = "minor"  # worth fixing, but not blocking
    NIT = "nit"  # style or naming preference

    @property
    def rank(self) -> int:
        return list(Severity).index(self)  # 0 = most severe


class Category(StrEnum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    MAINTAINABILITY = "maintainability"
    STYLE = "style"
    TESTING = "testing"


class Verdict(StrEnum):
    APPROVE = "approve"
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


class ReviewInput(AgentInput):
    """Review either a pasted diff or the uncommitted changes in a repository."""

    diff: str | None = Field(default=None, description="Unified diff to review")
    repo_path: str | None = Field(
        default=None,
        description="Path to a git repository; the agent reads the diff itself with tools",
    )
    base: str = Field(default="HEAD", description="Ref to diff against when repo_path is set")
    focus: list[Category] = Field(
        default_factory=list, description="Categories to concentrate on (empty = all)"
    )
    max_findings: int = Field(default=15, ge=1, le=50)

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if bool(self.diff and self.diff.strip()) == bool(self.repo_path):
            raise ValueError("provide exactly one of 'diff' or 'repo_path'")
        return self


class Finding(Contract):
    file: str = Field(min_length=1, description="Path as shown in the diff, e.g. 'app/db.py'")
    line: int | None = Field(default=None, ge=1, description="Line number in the new file")
    severity: Severity
    category: Category
    title: str = Field(min_length=3, max_length=120)
    explanation: str = Field(min_length=10, description="Why this is a problem")
    suggestion: str | None = Field(default=None, description="Concrete fix, code if helpful")


class ReviewOutput(AgentOutput):
    summary: str = Field(min_length=10, description="Two or three sentences on the change")
    verdict: Verdict
    findings: list[Finding] = Field(default_factory=list)

    def worst(self) -> Severity | None:
        return min((f.severity for f in self.findings), key=lambda s: s.rank, default=None)
