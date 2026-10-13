"""Input and output contracts for the Security Audit agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract

type Category = Literal[
    "sql_injection",
    "command_injection",
    "code_injection",
    "path_traversal",
    "ssrf",
    "xss",
    "hardcoded_secret",
    "weak_crypto",
    "insecure_deserialization",
    "insecure_transport",
    "authn",
    "authz",
    "misconfiguration",
    "input_validation",
    "info_disclosure",
    "other",
]


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    NEEDS_REVIEW = "needs_review"


class ScanLevel(StrEnum):
    """The lowest scanner severity worth looking at."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SecurityAuditInput(AgentInput):
    """Code to audit: pasted files, or a repository path."""

    files: dict[str, str] = Field(
        default_factory=dict, description="Source and config files: path -> content"
    )
    repo_path: str | None = Field(
        default=None, description="Repository to read (read-only) instead of pasted files"
    )
    paths: list[str] = Field(
        default_factory=list, description="Only audit these files or folders (repo mode)"
    )
    min_severity: ScanLevel = Field(
        default=ScanLevel.LOW, description="Ignore scanner findings below this severity"
    )
    instructions: str | None = Field(
        default=None, description="What to focus on, e.g. 'the payment flow'"
    )

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if bool(self.files) == bool(self.repo_path):
            raise ValueError("provide exactly one of 'files' or 'repo_path'")
        return self


class Triage(Contract):
    """The model's decision on one scanner finding."""

    finding_id: str = Field(min_length=2, description="The scanner finding's id, e.g. S3")
    verdict: Verdict
    severity: Severity = Field(description="Real-world severity if exploited here")
    reason: str = Field(
        min_length=10, description="Why: what an attacker can do, or why it is safe"
    )
    fix: str | None = Field(default=None, description="The concrete change that removes the risk")


class Additional(Contract):
    """A vulnerability the scanners missed, backed by a quote from the code."""

    file: str = Field(min_length=1)
    line: int = Field(ge=1, description="Line of the vulnerable statement")
    category: Category
    severity: Severity
    confidence: Literal["low", "medium", "high"] = "medium"
    title: str = Field(min_length=5)
    description: str = Field(min_length=20, description="How it can be exploited")
    evidence: str = Field(min_length=3, description="The vulnerable code, copied exactly")
    fix: str = Field(min_length=5, description="The concrete change that removes the risk")
    cwe: int | None = None


class Finding(Contract):
    """One entry of the final, de-duplicated, prioritised report."""

    id: str
    source: Literal["scanner", "llm"]
    rule: str = Field(description="Scanner rule id(s), or 'llm'")
    category: str
    severity: Severity
    status: Verdict
    confidence: str
    file: str
    line: int
    title: str
    description: str
    evidence: str
    fix: str | None = None
    cwe: int | None = None


class SecurityAuditOutput(AgentOutput):
    summary: str = Field(min_length=10, description="Two or three sentences for a busy reader")
    triage: list[Triage] = Field(
        default_factory=list, description="One entry for every scanner finding, none left out"
    )
    additional: list[Additional] = Field(
        default_factory=list, description="Findings scanners missed"
    )
    # Filled in by AgentForge from the checked triage; the model leaves these alone.
    findings: list[Finding] = Field(default_factory=list, description="Open findings, by priority")
    dismissed: list[Finding] = Field(default_factory=list, description="Scanner false positives")
    risk: Literal["none", "low", "medium", "high", "critical"] = "none"
    scanner_findings: int = Field(default=0, description="How many raw scanner findings there were")
