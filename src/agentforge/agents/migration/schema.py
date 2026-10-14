"""Input and output contracts for the Migration agent."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract
from agentforge.tools.py_migrate import RULES
from agentforge.tools.pytest_runner import PytestRun


class MigrationInput(AgentInput):
    """Python code to bring up to date with Python 3.12, and the tests that prove it still works."""

    files: dict[str, str] = Field(
        default_factory=dict,
        description="Python files: path -> content. Include the tests "
        "(files named test_*.py or *_test.py are run)",
    )
    repo_path: str | None = Field(
        default=None, description="Repository to read (read-only) instead of pasted files"
    )
    paths: list[str] = Field(
        default_factory=list, description="Only migrate these files or folders (repo mode)"
    )
    rules: list[str] = Field(
        default_factory=list,
        description=f"Migration rules to apply (default: all of {', '.join(RULES)})",
    )
    tests: list[str] = Field(
        default_factory=list,
        description="Test files to run, relative to the repository (repo mode). "
        "Default: the test files among the migrated files",
    )
    instructions: str | None = Field(
        default=None, description="Constraints, e.g. 'datetimes must stay naive'"
    )

    @field_validator("rules")
    @classmethod
    def _known_rules(cls, value: list[str]) -> list[str]:
        unknown = [r for r in value if r not in RULES]
        if unknown:
            raise ValueError(
                f"unknown rule(s) {', '.join(unknown)}; choose from {', '.join(RULES)}"
            )
        return value

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if bool(self.files) == bool(self.repo_path):
            raise ValueError("provide exactly one of 'files' or 'repo_path'")
        return self


class Step(Contract):
    """One step of the migration plan."""

    title: str = Field(min_length=5)
    risk: Literal["low", "medium", "high"]
    files: list[str] = Field(min_length=1, description="Files this step changes")
    sites: list[str] = Field(default_factory=list, description="Site ids this step resolves")
    rationale: str = Field(min_length=10, description="What changes and why it is safe")


class Skip(Contract):
    """A site deliberately left as it is."""

    site_id: str = Field(min_length=2)
    reason: str = Field(min_length=10, description="Why it cannot be migrated safely")


class SiteInfo(Contract):
    rule: str
    file: str
    line: int
    code: str
    detail: str


class MigrationOutput(AgentOutput):
    summary: str = Field(min_length=10, description="What was migrated, in one or two lines")
    plan: list[Step] = Field(default_factory=list, description="Ordered steps, safest first")
    files: dict[str, str] = Field(
        default_factory=dict,
        description="The complete new content of every CHANGED file: path -> source. "
        "Leave unchanged files out",
    )
    skipped: list[Skip] = Field(default_factory=list)
    manual_steps: list[str] = Field(
        default_factory=list, description="Things to do by hand, e.g. update a dependency"
    )
    # Filled in by AgentForge from the checked result; the model leaves these alone.
    changed: list[str] = Field(default_factory=list)
    diff: str = ""
    sites_before: list[SiteInfo] = Field(default_factory=list)
    sites_after: list[SiteInfo] = Field(default_factory=list)
    compiled: bool = False
    tests_before: PytestRun | None = None
    tests_after: PytestRun | None = None
    behaviour_verified: bool = Field(
        default=False, description="The tests pass on the migrated code (False when none exist)"
    )
