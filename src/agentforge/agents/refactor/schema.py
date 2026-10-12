"""Input and output contracts for the Refactoring agent."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Self

from pydantic import Field, field_validator, model_validator

from agentforge.agents.refactor.smells import Smell, Thresholds
from agentforge.core.schemas import AgentInput, AgentOutput
from agentforge.tools.pytest_runner import PytestRun


class RefactorInput(AgentInput):
    """A module to simplify, and the tests that prove its behaviour stays the same."""

    target: str = Field(
        min_length=1, description="Path of the module to refactor, relative to the project root"
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description="Source files: path -> content. Include the target and its tests "
        "(files named test_*.py or *_test.py are run)",
    )
    repo_path: str | None = Field(
        default=None, description="Repository to read with read-only tools instead of files"
    )
    tests: list[str] = Field(
        default_factory=list,
        description="Test files to run, relative to the repository (repo mode). "
        "Default: test files that mention the module",
    )
    thresholds: Thresholds = Field(default_factory=Thresholds)
    instructions: str | None = Field(default=None, description="Anything to focus on")
    allow_untested: bool = Field(
        default=False,
        description="Refactor even if no tests are found. Behaviour is then NOT verified",
    )

    @field_validator("target")
    @classmethod
    def _python_module(cls, value: str) -> str:
        value = value.replace("\\", "/").removeprefix("./")
        if not value.endswith(".py") or ".." in PurePosixPath(value).parts:
            raise ValueError("target must be a relative path to a .py file")
        return value

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if bool(self.files) == bool(self.repo_path):
            raise ValueError("provide exactly one of 'files' or 'repo_path'")
        if self.files and self.target not in self.files:
            raise ValueError(f"target '{self.target}' is not in files")
        return self


class RefactorOutput(AgentOutput):
    summary: str = Field(min_length=10, description="What was simplified, in one or two lines")
    changes: list[str] = Field(
        description="One short line per refactoring step (e.g. 'extracted _validate from checkout')"
    )
    refactored_source: str = Field(
        min_length=1, description="The complete module after refactoring, not a diff"
    )
    diff: str = Field(default="", description="Set by AgentForge: unified diff of the change")
    smells_before: list[Smell] = Field(default_factory=list, description="Set by AgentForge")
    smells_after: list[Smell] = Field(default_factory=list, description="Set by AgentForge")
    api_preserved: bool = Field(default=True, description="Set by AgentForge")
    tests_before: PytestRun | None = Field(default=None, description="Set by AgentForge")
    tests_after: PytestRun | None = Field(default=None, description="Set by AgentForge")
    behaviour_verified: bool = Field(
        default=False, description="Set by AgentForge: the tests passed before and after"
    )
