"""Golden eval cases for the Performance agent, and how a report is scored.

Each case in ``evals/*.json`` is a small codebase with slow code planted in it (and look-alike
code that is fine). The answer key says:

- ``find``: a slowdown that must be reported (file, line, function, accepted kinds and the
  lowest acceptable impact),
- ``safe``: a line that resembles a slow pattern but is fine; reporting it is a false alarm,
- ``bottleneck`` (profiled cases): the function that really takes most of the time.

Scoring uses only the final report:

1. every planted slowdown is among the open findings (same line or same function, accepted kind),
2. nothing open points at a safe line,
3. no two findings share file, line and kind,
4. findings are sorted by impact,
5. every confirmed finding has a fix,
6. the evidence of every finding really is in the file,
7. for profiled cases: the profile ran and the named bottleneck is the report's bottleneck.

Deterministic, no LLM judge.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.performance.schema import (
    Impact,
    PerformanceInput,
    PerformanceOutput,
    Verdict,
)
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check

LINE_TOLERANCE = 1
_RANK = {Impact.LOW: 1, Impact.MEDIUM: 2, Impact.HIGH: 3}


class Planted(Contract):
    file: str
    line: int = Field(ge=1)
    function: str = ""
    kind: list[str] = Field(min_length=1)
    min_impact: Impact = Impact.MEDIUM


class SafeLine(Contract):
    file: str
    line: int = Field(ge=1)


class Expectations(Contract):
    find: list[Planted] = Field(default_factory=list)
    safe: list[SafeLine] = Field(default_factory=list)
    bottleneck: str | None = None


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def perf_input(self) -> PerformanceInput:
        return PerformanceInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.performance").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def covers(planted: Planted, file: str, line: int, function: str, kind: str) -> bool:
    """Does a finding at this place and of this kind answer the planted slowdown?"""
    if file != planted.file or kind not in planted.kind:
        return False
    same_function = bool(planted.function) and function.rsplit(".", 1)[-1] == planted.function
    return same_function or abs(line - planted.line) <= LINE_TOLERANCE


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def score(case: EvalCase, output: PerformanceOutput) -> CaseScore:
    files: dict[str, str] = case.input["files"]
    open_findings = output.findings
    checks: list[Check] = []

    for p in case.expect.find:
        hit = next(
            (f for f in open_findings if covers(p, f.file, f.line, f.function, f.kind)), None
        )
        where = f"{p.file}:{p.line} ({'/'.join(p.kind)})"
        if hit is None:
            checks.append(Check(name=f"finds {where}", passed=False, detail="not reported"))
            continue
        enough = _RANK[hit.impact] >= _RANK[p.min_impact]
        detail = "" if enough else f"{hit.impact.value}, expected {p.min_impact.value}+"
        checks.append(Check(name=f"finds {where}", passed=enough, detail=detail))

    false_alarms = [
        f"{f.file}:{f.line}"
        for f in open_findings
        for s in case.expect.safe
        if f.file == s.file and f.line == s.line
    ]
    checks.append(
        Check(name="no false alarms", passed=not false_alarms, detail=", ".join(false_alarms[:3]))
    )

    keys = [(f.file, f.line, f.kind) for f in open_findings]
    repeated = len(keys) - len(set(keys))
    checks.append(Check(name="no duplicates", passed=repeated == 0, detail=f"{repeated} repeated"))

    ranks = [_RANK[f.impact] for f in open_findings]
    checks.append(Check(name="sorted by impact", passed=ranks == sorted(ranks, reverse=True)))

    unfixed = [f.id for f in open_findings if f.status is Verdict.CONFIRMED and not f.fix]
    checks.append(
        Check(
            name="confirmed findings have a fix", passed=not unfixed, detail=", ".join(unfixed[:3])
        )
    )

    fake = [
        f.id
        for f in [*open_findings, *output.dismissed]
        if _norm(f.evidence) not in _norm(files.get(f.file, ""))
    ]
    checks.append(
        Check(name="evidence is in the code", passed=not fake, detail=", ".join(fake[:3]))
    )

    if case.expect.bottleneck:
        wanted = case.expect.bottleneck
        got = output.bottleneck or "none"
        checks.append(
            Check(
                name="profile ran",
                passed=output.profiled and bool(output.hotspots),
                detail=f"{len(output.hotspots)} hotspot(s)",
            )
        )
        checks.append(
            Check(
                name=f"bottleneck is {wanted}",
                passed=got.rsplit(":", 1)[-1].rsplit(".", 1)[-1] == wanted,
                detail=got,
            )
        )
    return CaseScore(case=case.name, checks=checks)
