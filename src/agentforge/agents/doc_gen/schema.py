"""Input and output contracts for the Documentation agent."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self

from pydantic import Field, field_validator, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract
from agentforge.tools.pytest_runner import PytestRun


class DocStyle(StrEnum):
    GOOGLE = "google"
    NUMPY = "numpy"
    SPHINX = "sphinx"


class DocGenInput(AgentInput):
    """A module to document: either pasted files or a path inside a repository."""

    target: str = Field(
        min_length=1, description="Path of the module to document, relative to the project root"
    )
    files: dict[str, str] = Field(
        default_factory=dict, description="Source files: path -> content (must include target)"
    )
    repo_path: str | None = Field(
        default=None, description="Repository to read with read-only tools instead of files"
    )
    style: DocStyle = Field(default=DocStyle.GOOGLE, description="Docstring convention")
    rewrite_existing: bool = Field(
        default=False, description="Also rewrite docstrings that already exist"
    )
    readme: bool = Field(default=True, description="Also write a Markdown usage guide")
    instructions: str | None = Field(default=None, description="Anything to focus on")

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


class Docstring(Contract):
    name: str = Field(
        min_length=1, description="Qualified name (e.g. 'Cart.add'), or '<module>' for the module"
    )
    docstring: str = Field(min_length=3, description="The docstring text, without quotes")


class DocGenOutput(AgentOutput):
    summary: str = Field(min_length=10, description="What the module does, in one or two lines")
    docstrings: list[Docstring] = Field(description="New docstrings, one per definition")
    readme: str | None = Field(
        default=None, description="Markdown usage guide with examples (when asked for)"
    )
    documented_source: str = Field(
        default="", description="Set by AgentForge: the module with the docstrings applied"
    )
    coverage_before: float = Field(default=0.0, description="Set by AgentForge (percent)")
    coverage_after: float = Field(default=0.0, description="Set by AgentForge (percent)")
    doctests: PytestRun | None = Field(
        default=None, description="Set by AgentForge when docstrings contain >>> examples"
    )
