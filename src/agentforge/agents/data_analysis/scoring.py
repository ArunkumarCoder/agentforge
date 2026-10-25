"""Golden eval cases for the Data Analysis agent, and how an analysis is scored.

Each case in ``evals/*.json`` is a question about a few small data files, with traps in the
data (cancelled orders, blank readings, a second file to join). The expected numbers were
computed with pandas from the same files. Scoring uses only the final report:

1. every expected number appears among the findings or in the answer, within a tolerance,
2. every expected word appears in the answer (for example the name of the top region),
3. every finding's value equals what the script emitted,
4. at least as many charts as the case needs, each a real PNG,
5. the script emitted something (results are not empty).

Deterministic, no LLM judge.
"""

from __future__ import annotations

import base64
import json
import re
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.data_analysis.schema import DataAnalysisInput, DataAnalysisOutput
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_NUMBER = re.compile(r"-?\d[\d,]*\.?\d*")


class ExpectedNumber(Contract):
    name: str
    value: float
    tol: float = Field(default=0.01, ge=0)


class Expectations(Contract):
    numbers: list[ExpectedNumber] = Field(default_factory=list)
    words: list[str] = Field(default_factory=list)
    charts_min: int = Field(default=0, ge=0)


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def analysis_input(self) -> DataAnalysisInput:
        return DataAnalysisInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.data_analysis").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def _numbers_in(text: str) -> list[float]:
    found = []
    for token in _NUMBER.findall(text):
        try:
            found.append(float(token.replace(",", "").rstrip(".")))
        except ValueError:
            continue
    return found


def _reported_numbers(output: DataAnalysisOutput) -> list[float]:
    values = [
        float(f.value)
        for f in output.findings
        if isinstance(f.value, int | float) and not isinstance(f.value, bool)
    ]
    return values + _numbers_in(output.answer)


def score(case: EvalCase, output: DataAnalysisOutput) -> CaseScore:
    reported = _reported_numbers(output)
    answer = output.answer.lower()
    checks: list[Check] = []

    for n in case.expect.numbers:
        hit = any(abs(x - n.value) <= n.tol for x in reported)
        checks.append(Check(name=f"reports {n.name} ({n.value:g})", passed=hit))

    for word in case.expect.words:
        checks.append(Check(name=f"names {word!r}", passed=word.lower() in answer))

    mismatched = [
        f.key
        for f in output.findings
        if f.key not in output.results or not _same(f.value, output.results[f.key])
    ]
    checks.append(
        Check(
            name="findings equal the emitted values",
            passed=not mismatched,
            detail=", ".join(mismatched[:3]),
        )
    )

    good_charts = 0
    for chart in output.chart_files:
        try:
            if base64.b64decode(chart.png_base64).startswith(PNG_MAGIC):
                good_charts += 1
        except ValueError:
            continue
    checks.append(
        Check(
            name="charts are real images",
            passed=good_charts >= case.expect.charts_min and good_charts == len(output.chart_files),
            detail=f"{good_charts} of {case.expect.charts_min} needed",
        )
    )
    checks.append(Check(name="the script emitted results", passed=bool(output.results)))
    return CaseScore(case=case.name, checks=checks)


def _same(a: object, b: object) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(a - b) <= 1e-6 * max(1.0, abs(b))
    return a == b
