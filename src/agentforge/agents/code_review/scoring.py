"""Golden eval cases for the Code Review agent, and how a review is scored against one.

Each case in ``evals/*.json`` has an input (a diff) and expectations:

- ``verdict_in``: acceptable verdicts,
- ``must_find``: problems the review must report (file, category, minimum
  severity, optional keywords and approximate line),
- ``max_severity``: nothing worse than this may be reported (for clean changes),
- ``max_findings``: an upper bound, to catch noisy reviews.

Scoring is deterministic: no LLM judge. The eval runner (Day 15) runs these
cases against real models and records pass rates.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.code_review.schema import (
    Category,
    Finding,
    ReviewInput,
    ReviewOutput,
    Severity,
    Verdict,
)
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check

LINE_TOLERANCE = 3


class ExpectedFinding(Contract):
    file: str
    categories: list[Category] = Field(min_length=1)
    min_severity: Severity = Severity.MAJOR
    keywords_any: list[str] = Field(default_factory=list)
    near_line: int | None = None


class Expectations(Contract):
    verdict_in: list[Verdict] = Field(min_length=1)
    must_find: list[ExpectedFinding] = Field(default_factory=list)
    max_severity: Severity | None = None
    max_findings: int | None = None


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def review_input(self) -> ReviewInput:
        return ReviewInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.code_review").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def _matches(expected: ExpectedFinding, finding: Finding) -> bool:
    if finding.file != expected.file or finding.category not in expected.categories:
        return False
    if finding.severity.rank > expected.min_severity.rank:
        return False
    text = f"{finding.title} {finding.explanation} {finding.suggestion or ''}".lower()
    if expected.keywords_any and not any(k.lower() in text for k in expected.keywords_any):
        return False
    return not (
        expected.near_line is not None
        and finding.line is not None
        and abs(finding.line - expected.near_line) > LINE_TOLERANCE
    )


def score(case: EvalCase, output: ReviewOutput) -> CaseScore:
    expect = case.expect
    wanted = ", ".join(v.value for v in expect.verdict_in)
    checks = [
        Check(
            name="verdict",
            passed=output.verdict in expect.verdict_in,
            detail=f"got {output.verdict.value}, want one of: {wanted}",
        )
    ]
    for i, expected in enumerate(expect.must_find, 1):
        found = any(_matches(expected, f) for f in output.findings)
        checks.append(
            Check(
                name=f"finds #{i}: {expected.categories[0].value} in {expected.file}",
                passed=found,
                detail="" if found else f"no {expected.min_severity.value}+ finding matched",
            )
        )
    if expect.max_severity is not None:
        worst = output.worst()
        ok = worst is None or worst.rank >= expect.max_severity.rank
        checks.append(
            Check(
                name=f"nothing worse than {expect.max_severity.value}",
                passed=ok,
                detail=f"worst was {worst.value if worst else 'none'}",
            )
        )
    if expect.max_findings is not None:
        count = len(output.findings)
        checks.append(
            Check(
                name=f"at most {expect.max_findings} findings",
                passed=count <= expect.max_findings,
                detail=f"got {count}",
            )
        )
    return CaseScore(case=case.name, checks=checks)
