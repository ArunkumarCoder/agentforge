"""Golden eval cases for the Security Audit agent, and how an audit is scored.

Each case in ``evals/*.json`` is a small codebase with vulnerabilities planted in it. The
code carries the answer key, which is turned into expectations:

- ``find``: a vulnerability that must be reported (file, line, accepted categories, and the
  lowest acceptable severity),
- ``safe``: a line that looks suspicious to a scanner but is fine; reporting it is a false alarm.

Scoring uses only the final report, not the agent's own claims:

1. every planted vulnerability is among the open findings (line within one, right category),
2. nothing open points at a safe line,
3. no two findings share file, line and category,
4. findings are sorted by severity,
5. every confirmed finding has a fix,
6. the evidence of every finding really is in the file,
7. the reported risk equals the worst open finding.

Deterministic, no LLM judge.
"""

from __future__ import annotations

import json
import re
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.security_audit.schema import (
    SecurityAuditInput,
    SecurityAuditOutput,
    Severity,
    Verdict,
)
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check

LINE_TOLERANCE = 1
_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class Planted(Contract):
    file: str
    line: int = Field(ge=1)
    category: list[str] = Field(min_length=1)
    min_severity: Severity = Severity.MEDIUM


class SafeLine(Contract):
    file: str
    line: int = Field(ge=1)


class Expectations(Contract):
    find: list[Planted] = Field(default_factory=list)
    safe: list[SafeLine] = Field(default_factory=list)


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def audit_input(self) -> SecurityAuditInput:
        return SecurityAuditInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.security_audit").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def score(case: EvalCase, output: SecurityAuditOutput) -> CaseScore:
    files: dict[str, str] = case.input["files"]
    open_findings = output.findings
    checks: list[Check] = []

    for p in case.expect.find:
        hit = next(
            (
                f
                for f in open_findings
                if f.file == p.file
                and abs(f.line - p.line) <= LINE_TOLERANCE
                and f.category in p.category
            ),
            None,
        )
        where = f"{p.file}:{p.line} ({'/'.join(p.category)})"
        if hit is None:
            checks.append(Check(name=f"finds {where}", passed=False, detail="not reported"))
        else:
            enough = _RANK[hit.severity] >= _RANK[p.min_severity]
            checks.append(
                Check(
                    name=f"finds {where}",
                    passed=enough,
                    detail=""
                    if enough
                    else f"{hit.severity.value}, expected {p.min_severity.value}+",
                )
            )

    false_alarms = [
        f"{f.file}:{f.line}"
        for f in open_findings
        for s in case.expect.safe
        if f.file == s.file and abs(f.line - s.line) <= 0
    ]
    checks.append(
        Check(name="no false alarms", passed=not false_alarms, detail=", ".join(false_alarms[:3]))
    )

    keys = [(f.file, f.line, f.category) for f in open_findings]
    duplicates = len(keys) - len(set(keys))
    checks.append(
        Check(name="no duplicates", passed=duplicates == 0, detail=f"{duplicates} repeated")
    )

    ranks = [_RANK[f.severity] for f in open_findings]
    checks.append(Check(name="sorted by severity", passed=ranks == sorted(ranks, reverse=True)))

    unfixed = [f.id for f in open_findings if f.status is Verdict.CONFIRMED and not f.fix]
    checks.append(
        Check(
            name="confirmed findings have a fix", passed=not unfixed, detail=", ".join(unfixed[:3])
        )
    )

    fake = [
        f.id
        for f in [*open_findings, *output.dismissed]
        if _norm(f.evidence.split("…")[0]) not in _norm(files.get(f.file, ""))
    ]
    checks.append(
        Check(name="evidence is in the code", passed=not fake, detail=", ".join(fake[:3]))
    )

    worst = (
        max(open_findings, key=lambda f: _RANK[f.severity]).severity.value
        if open_findings
        else "none"
    )
    checks.append(
        Check(
            name="risk matches the worst finding",
            passed=output.risk == worst,
            detail=f"{output.risk} vs {worst}",
        )
    )
    return CaseScore(case=case.name, checks=checks)
