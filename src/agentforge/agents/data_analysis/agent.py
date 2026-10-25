"""Agent 12 — Data Analysis: the model writes pandas code, AgentForge runs it and checks it.

The data files are profiled first (shape, columns, types, missing values, a few rows) and the
profile goes into the prompt. The model explores with ``run_python``: a complete script per
call, run in a throw-away folder with no network and CPU, memory, file and time limits.

The final answer carries the **complete script** and a list of findings, each tied to a value
the script ``emit``-s. AgentForge then runs that script again, from scratch, and accepts the
answer only if

- the script runs and finishes,
- every finding's ``key`` was emitted, and the finding's ``value`` equals the emitted value,
- the finding's sentence really states that number, and
- every declared chart was saved as a real PNG.

So a number in the report is a number the code computed, not one the model remembered or
guessed. The model gets two corrections.
"""

from __future__ import annotations

import base64
import math
import re
from typing import Any, ClassVar

from agentforge.agents.data_analysis.schema import (
    ChartFile,
    DataAnalysisInput,
    DataAnalysisOutput,
    Finding,
)
from agentforge.core.agent import RunContext
from agentforge.core.errors import AgentForgeError
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools.base import ToolError, ToolPermission
from agentforge.tools.builtin import Workspace
from agentforge.tools.pyexec import PyRun, RunPythonTool, run_python, safe_relative
from agentforge.tools.registry import ToolRegistry
from agentforge.tools.sandbox import untrusted_limits

DATA_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".txt", ".ndjson"}
MAX_FILES = 20
MAX_FILE_BYTES = 5_000_000
MAX_TOTAL_BYTES = 10_000_000
RUN_TIMEOUT_S = 60.0
REL_TOL = 1e-6
ABS_TOL = 1e-9
CLAIM_TOLERANCE = 0.01
MAX_PROFILE_CHARS = 9000
MAX_SHOWN = 8

# Reads every data file it can and prints what a person would look at first.
_PROFILE = r"""
import glob, os
import pandas as pd
pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)
for path in sorted(glob.glob("**/*", recursive=True)):
    if not os.path.isfile(path) or path == "user_code.py":
        continue
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".csv":
            df = pd.read_csv(path)
        elif ext == ".tsv":
            df = pd.read_csv(path, sep="\t")
        elif ext in (".json",):
            df = pd.read_json(path)
        elif ext in (".jsonl", ".ndjson"):
            df = pd.read_json(path, lines=True)
        else:
            continue
    except Exception as exc:
        print(f"## {path}\ncould not be read as a table: {type(exc).__name__}: {exc}\n")
        continue
    print(f"## {path}: {len(df)} rows x {len(df.columns)} columns")
    for col in df.columns:
        s = df[col]
        extra = ""
        if pd.api.types.is_numeric_dtype(s):
            extra = f", min {s.min()}, max {s.max()}"
        elif s.nunique() <= 8:
            extra = ", values " + ", ".join(repr(v) for v in s.dropna().unique()[:8])
        print(f"- {col}: {s.dtype}, {int(s.isna().sum())} missing{extra}")
    print(df.head(3).to_string())
    print()
"""


class DataAnalysisError(AgentForgeError):
    """The analysis cannot start (no data files, pandas missing)."""


@register_agent
class DataAnalysisAgent(LLMAgent[DataAnalysisInput, DataAnalysisOutput]):
    name = "data-analysis"
    description = (
        "Answers a question about CSV/JSON data by writing pandas code, running it in a sandbox "
        "(no network, time, memory and file limits), and returning findings whose numbers are "
        "checked against a fresh run of the final script, plus charts."
    )
    version = "0.1.0"
    tags = ("data", "code")
    input_model = DataAnalysisInput
    output_model = DataAnalysisOutput
    allowed_permissions: ClassVar[frozenset[ToolPermission]] = frozenset(
        {ToolPermission.READ, ToolPermission.EXEC}
    )
    max_steps = 12
    max_tokens = 6000
    max_repairs = 2
    timeout_s = 600.0

    # ------------------------------------------------------------------ setup

    async def before_run(self, data: DataAnalysisInput, ctx: RunContext) -> None:
        files = self._collect(data)
        if not files:
            raise DataAnalysisError("no readable data files (csv, tsv, json, jsonl) to analyse")
        limits = untrusted_limits()
        profile = await run_python(files, _PROFILE, timeout_s=RUN_TIMEOUT_S, limits=limits)
        if not profile.ok:
            if "ModuleNotFoundError" in profile.process.stderr:
                raise DataAnalysisError(
                    "pandas and matplotlib are needed for data analysis: "
                    "uv sync --extra analysis (or pip install 'agentforge[analysis]')"
                )
            raise DataAnalysisError(
                "the data could not be profiled: " + profile.process.render()[-500:]
            )
        text = profile.process.stdout.strip()
        if len(text) > MAX_PROFILE_CHARS:
            text = text[:MAX_PROFILE_CHARS] + "\n...[profile cut]"
        ctx.metadata.update(files=files, profile=text, limits=limits)

    # ------------------------------------------------------------------ prompt

    def build_user_message(self, data: DataAnalysisInput, ctx: RunContext) -> str:
        files: dict[str, str] = ctx.metadata["files"]
        parts = [f"## Question\n\n{data.question.strip()}"]
        if data.instructions:
            parts.append(f"\n## Scope and style\n\n{data.instructions.strip()}")
        parts.append(
            f"\n## Data files ({len(files)}), profiled\n\nThe files are in the script's working "
            f"folder under these names: {', '.join(sorted(files))}.\n\n{ctx.metadata['profile']}"
        )
        return "\n".join(parts)

    def build_tools(self, data: DataAnalysisInput, ctx: RunContext) -> ToolRegistry | None:
        return ToolRegistry(
            [
                RunPythonTool(
                    ctx.metadata["files"],
                    limits=ctx.metadata["limits"],
                    run_timeout_s=RUN_TIMEOUT_S,
                )
            ]
        )

    # ------------------------------------------------------------------ verification

    async def check(
        self, output: DataAnalysisOutput, data: DataAnalysisInput, ctx: RunContext
    ) -> None:
        run = await run_python(
            ctx.metadata["files"],
            output.code,
            timeout_s=RUN_TIMEOUT_S,
            limits=ctx.metadata["limits"],
        )
        ctx.metadata["final_run"] = run
        problems = run_problems(output, run)
        if problems:
            raise OutputCheckError(
                "Your final script or findings have problems:\n"
                + "\n".join(f"- {p}" for p in problems[:MAX_SHOWN])
                + "\nFix them and return the complete JSON again."
            )

    def postprocess(
        self, output: DataAnalysisOutput, data: DataAnalysisInput, ctx: RunContext
    ) -> DataAnalysisOutput:
        run: PyRun = ctx.metadata["final_run"]
        files = [
            ChartFile(
                name=c.name,
                title=c.title,
                png_base64=base64.b64encode(run.charts[c.name]).decode("ascii"),
                bytes=len(run.charts[c.name]),
            )
            for c in output.charts
        ]
        return output.model_copy(
            update={"results": run.results, "chart_files": files, "isolation": run.isolation}
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _collect(data: DataAnalysisInput) -> dict[str, str]:
        if not data.repo_path:
            found = dict(data.files)
            for name in found:
                safe_relative(name)
            _check_size(found)
            return found
        workspace = Workspace(data.repo_path)
        roots = [workspace.resolve(p) for p in data.paths] or [workspace.root]
        found = {}
        for root in roots:
            candidates = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in candidates:
                if len(found) >= MAX_FILES:
                    break
                if path.is_symlink() or not path.is_file():
                    continue
                if workspace.is_ignored(path) or workspace.is_denied(path):
                    continue
                if path.suffix.lower() not in DATA_SUFFIXES or path.stat().st_size > MAX_FILE_BYTES:
                    continue
                try:
                    found[workspace.relative(path)] = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        _check_size(found)
        return found


def _check_size(files: dict[str, str]) -> None:
    if len(files) > MAX_FILES:
        raise DataAnalysisError(f"{len(files)} files is more than the {MAX_FILES} allowed")
    total = sum(len(t.encode("utf-8")) for t in files.values())
    if total > MAX_TOTAL_BYTES or any(len(t) > MAX_FILE_BYTES for t in files.values()):
        raise DataAnalysisError(
            f"the data is too large (limit {MAX_FILE_BYTES // 1_000_000} MB per file, "
            f"{MAX_TOTAL_BYTES // 1_000_000} MB in total)"
        )
    for name in files:
        try:
            safe_relative(name)
        except ToolError as exc:
            raise DataAnalysisError(str(exc)) from exc


# ---------------------------------------------------------------------- checks


def run_problems(output: DataAnalysisOutput, run: PyRun) -> list[str]:
    """Everything wrong with the final script's run and with the findings (``[]``: all good)."""
    if run.process.timed_out:
        return ["the final script timed out; make it faster (fewer passes, no huge loops)"]
    if run.process.exit_code != 0:
        return ["the final script failed:\n" + run.process.render()[-1500:]]
    problems: list[str] = []
    for f in output.findings:
        if f.key not in run.results:
            known = ", ".join(sorted(run.results)[:10]) or "nothing"
            problems.append(f"finding '{f.key}': the script never emitted it (it emitted: {known})")
            continue
        emitted = run.results[f.key]
        if not values_match(f.value, emitted):
            problems.append(
                f"finding '{f.key}': you reported {f.value!r} but the script emitted {emitted!r}"
            )
        elif not claim_states(f.claim, f.value):
            problems.append(f"finding '{f.key}': the sentence does not contain {f.value!r}")
    declared = {c.name for c in output.charts}
    for c in output.charts:
        if c.name not in run.charts:
            saved = ", ".join(sorted(run.charts)) or "none"
            problems.append(f"chart '{c.name}' was not saved by the script (saved: {saved})")
    if len(declared) != len(output.charts):
        problems.append("chart names must be unique")
    return problems


def values_match(reported: object, emitted: object) -> bool:
    """Equal numbers (to a tiny tolerance), equal text, or equal booleans."""
    if isinstance(reported, bool) or isinstance(emitted, bool):
        return reported is emitted
    if isinstance(reported, int | float) and isinstance(emitted, int | float):
        return math.isclose(reported, emitted, rel_tol=REL_TOL, abs_tol=ABS_TOL)
    if isinstance(reported, str) and isinstance(emitted, str):
        return reported.strip() == emitted.strip()
    return False


_NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")


def claim_states(claim: str, value: Any) -> bool:
    """For a numeric value: the sentence contains that number (rounded, as a percent, with
    thousands separators). For text: the sentence contains the text."""
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        return value.strip().lower() in claim.lower()
    for token in _NUMBER.findall(claim):
        try:
            x = float(token.replace(",", "").rstrip("."))
        except ValueError:
            continue
        for candidate in (x, x / 100, x * 100, x * 1000, x * 1_000_000):
            if abs(candidate - value) <= max(CLAIM_TOLERANCE * abs(value), 0.01):
                return True
    return False


__all__ = [
    "DataAnalysisAgent",
    "DataAnalysisError",
    "Finding",
    "claim_states",
    "run_problems",
    "values_match",
]
