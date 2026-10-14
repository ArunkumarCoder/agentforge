"""Input and output contracts for the Performance agent."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract

type Kind = Literal[
    "nested_loop",
    "list_membership",
    "string_concat",
    "io_in_loop",
    "list_front_ops",
    "sort_in_loop",
    "regex_in_loop",
    "complexity",
    "hotspot",
    "algorithm",
    "caching",
    "memory",
    "other",
]


class Impact(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    NEEDS_REVIEW = "needs_review"


class PerformanceInput(AgentInput):
    """Python code to analyse: pasted files, or a repository path."""

    files: dict[str, str] = Field(
        default_factory=dict, description="Python source files: path -> content"
    )
    repo_path: str | None = Field(
        default=None, description="Repository to read (read-only) instead of pasted files"
    )
    paths: list[str] = Field(
        default_factory=list, description="Only analyse these files or folders (repo mode)"
    )
    profile_script: str | None = Field(
        default=None,
        description="A script to run under cProfile (path in the files or repo). It is "
        "executed in a separate process, so only name a script that is safe to run.",
    )
    instructions: str | None = Field(
        default=None, description="What to focus on, e.g. 'the import job'"
    )

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if bool(self.files) == bool(self.repo_path):
            raise ValueError("provide exactly one of 'files' or 'repo_path'")
        return self


class Triage(Contract):
    """The model's decision on one candidate."""

    finding_id: str = Field(min_length=2, description="The candidate's id, e.g. P3 or H1")
    verdict: Verdict
    impact: Impact = Field(description="How much it slows the real workload")
    reason: str = Field(min_length=10, description="Why it matters, or why it is harmless here")
    fix: str | None = Field(default=None, description="The concrete change, ideally a code snippet")


class Additional(Contract):
    """A slowdown the tools missed, backed by a quote from the code."""

    file: str = Field(min_length=1)
    line: int = Field(ge=1, description="Line of the slow statement (or the def line)")
    kind: Kind
    impact: Impact
    confidence: Literal["low", "medium", "high"] = "medium"
    title: str = Field(min_length=5)
    description: str = Field(min_length=20, description="Why it is slow and how it scales")
    evidence: str = Field(min_length=3, description="The slow code, copied exactly")
    fix: str = Field(min_length=5, description="The concrete change that removes the cost")


class Finding(Contract):
    """One entry of the final, de-duplicated, prioritised report."""

    id: str
    source: Literal["static", "profile", "llm"]
    kind: str
    impact: Impact
    status: Verdict
    file: str
    line: int
    function: str
    title: str
    description: str
    evidence: str
    fix: str | None = None
    share: float | None = Field(
        default=None, description="Share of measured run time in this function, 0..1"
    )


class HotspotInfo(Contract):
    file: str
    line: int
    function: str
    calls: int
    tottime_s: float
    cumtime_s: float
    share: float


class PerformanceOutput(AgentOutput):
    summary: str = Field(min_length=10, description="Two or three sentences, worst problem first")
    triage: list[Triage] = Field(
        default_factory=list, description="One entry for every candidate, none left out"
    )
    additional: list[Additional] = Field(default_factory=list, description="Slowdowns tools missed")
    # Filled in by AgentForge from the checked triage; the model leaves these alone.
    findings: list[Finding] = Field(default_factory=list, description="Open findings, by priority")
    dismissed: list[Finding] = Field(default_factory=list, description="Harmless candidates")
    hotspots: list[HotspotInfo] = Field(default_factory=list, description="Measured by cProfile")
    bottleneck: str | None = Field(default=None, description="'file:function' with the most time")
    candidates: int = Field(default=0, description="How many candidates the tools reported")
    profiled: bool = False
