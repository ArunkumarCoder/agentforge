"""Input and output contracts for the Test Generation agent."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Self

from pydantic import Field, field_validator, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract

TEST_FILE = re.compile(r"^(?:[\w-]+/)*test_\w+\.py$")


def import_name(path: str) -> str:
    """``src/shop/cart.py`` -> ``shop.cart``; ``shop/__init__.py`` -> ``shop``."""
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def is_test_file(path: str) -> bool:
    name = PurePosixPath(path).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


class GenerateTestsInput(AgentInput):
    """A module to test: either pasted files or a path inside a repository."""

    target: str = Field(
        min_length=1, description="Path of the module to test, relative to the project root"
    )
    files: dict[str, str] = Field(
        default_factory=dict,
        description="Source files: path -> content. Include the target, modules it imports, "
        "and any existing tests for it (used to measure coverage before)",
    )
    repo_path: str | None = Field(
        default=None, description="Repository to read with read-only tools instead of files"
    )
    instructions: str | None = Field(
        default=None, description="Anything to focus on, e.g. 'edge cases of parse()'"
    )
    max_attempts: int = Field(
        default=3, ge=1, le=5, description="Test runs allowed before giving up"
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


class PytestRun(Contract):
    """What happened when AgentForge ran the generated tests."""

    passed: bool
    tests: int = Field(ge=0)
    failures: int = Field(ge=0, description="Failed tests plus errors")
    skipped: int = Field(ge=0)
    coverage: float | None = Field(
        default=None, description="Percent of the target's statements run by the tests"
    )
    output: str = Field(default="", description="The end of pytest's output")


class GenerateTestsOutput(AgentOutput):
    test_path: str = Field(description="Where the test file goes, e.g. tests/test_cart.py")
    test_code: str = Field(min_length=20, description="The complete pytest file")
    summary: str = Field(min_length=10, description="What the tests cover, in one or two lines")
    cases: list[str] = Field(min_length=1, description="One short line per behaviour tested")
    suspected_bugs: list[str] = Field(
        default_factory=list,
        description="Behaviour that looks wrong; each one has a strict xfail test",
    )
    run: PytestRun | None = Field(
        default=None, description="Set by AgentForge after running the tests; leave null"
    )
    coverage_before: float | None = Field(
        default=None, description="Set by AgentForge (coverage from existing tests); leave null"
    )
    attempts: int = Field(default=0, ge=0, description="Set by AgentForge; leave 0")

    @field_validator("test_path")
    @classmethod
    def _test_file(cls, value: str) -> str:
        value = value.replace("\\", "/").removeprefix("./")
        if not TEST_FILE.match(value):
            raise ValueError("test_path must look like tests/test_<name>.py (no '..')")
        return value
