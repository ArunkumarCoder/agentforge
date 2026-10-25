"""Input and output contracts for the Data Analysis agent."""

from __future__ import annotations

from typing import Any, Self

from pydantic import Field, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract

type Scalar = float | int | str | bool


class DataAnalysisInput(AgentInput):
    """A question about data: pasted files, or files in a repository folder."""

    question: str = Field(min_length=5, description="What to find out from the data")
    files: dict[str, str] = Field(
        default_factory=dict, description="Data files as text (CSV, TSV, JSON): name -> content"
    )
    repo_path: str | None = Field(
        default=None, description="Folder to read data files from (read-only) instead of 'files'"
    )
    paths: list[str] = Field(
        default_factory=list, description="Only these files or folders (folder mode)"
    )
    instructions: str | None = Field(
        default=None, description="Scope or style, e.g. 'exclude cancelled orders'"
    )

    @model_validator(mode="after")
    def _one_source(self) -> Self:
        if bool(self.files) == bool(self.repo_path):
            raise ValueError("provide exactly one of 'files' or 'repo_path'")
        return self


class Finding(Contract):
    """One number or label the analysis produced, tied to a value its code emitted."""

    claim: str = Field(
        min_length=10, description="One sentence that states the value, e.g. 'Mean order: 63.4'"
    )
    key: str = Field(min_length=1, description="The name the code gave this value in emit()")
    value: Scalar = Field(description="The same value, exactly as the code emitted it")


class ChartSpec(Contract):
    name: str = Field(min_length=1, description="The name passed to save_chart()")
    title: str = Field(min_length=3, description="What the chart shows")


class ChartFile(Contract):
    name: str
    title: str
    png_base64: str
    bytes: int


class DataAnalysisOutput(AgentOutput):
    answer: str = Field(min_length=10, description="The answer in plain prose, with the numbers")
    code: str = Field(
        min_length=10,
        description="The complete final script. AgentForge runs it again to check the findings",
    )
    findings: list[Finding] = Field(min_length=1)
    charts: list[ChartSpec] = Field(default_factory=list, description="Charts the code saves")
    caveats: list[str] = Field(
        default_factory=list, description="Data problems that affect the answer"
    )
    # Filled in by AgentForge from the checked run.
    results: dict[str, Any] = Field(default_factory=dict, description="Everything the code emitted")
    chart_files: list[ChartFile] = Field(default_factory=list)
    isolation: str = Field(default="", description="namespace (no network at all) or guard")
