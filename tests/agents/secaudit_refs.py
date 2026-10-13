"""A perfect audit for each golden case, derived from the case's answer key.

The real scanners run over the case; every scanner finding on a planted line is confirmed,
every other one is dismissed with a reason, and each planted vulnerability no scanner
reported becomes an ``additional`` finding quoting its line. The result scores full marks,
so tests use it as the model's ideal answer and to prove the cases measure what they claim.
"""

import json
from typing import Any

from agentforge.agents.security_audit.scoring import EvalCase, Planted
from agentforge.tools import security_scan

FIX = "Use a parameterised or allow-listed alternative and read secrets from the environment."


def _planted(case: EvalCase, file: str, line: int, category: str) -> Planted | None:
    return next(
        (
            p
            for p in case.expect.find
            if p.file == file and p.line == line and category in p.category
        ),
        None,
    )


async def scanner_findings(case: EvalCase) -> list[security_scan.ScannerFinding]:
    return await security_scan.scan(
        case.input["files"], min_severity=case.input.get("min_severity", "low")
    )


async def reference_answer(case: EvalCase, **overrides: Any) -> dict[str, Any]:
    files: dict[str, str] = case.input["files"]
    raw = await scanner_findings(case)
    triage = []
    covered: set[tuple[str, int]] = set()
    for i, f in enumerate(raw, start=1):
        hit = _planted(case, f.file, f.line, f.category)
        if hit:
            covered.add((hit.file, hit.line))
            triage.append(
                {
                    "finding_id": f"S{i}",
                    "verdict": "confirmed",
                    "severity": hit.min_severity.value,
                    "reason": f"{f.title} An attacker controls the value used here.",
                    "fix": FIX,
                }
            )
        else:
            triage.append(
                {
                    "finding_id": f"S{i}",
                    "verdict": "false_positive",
                    "severity": "info",
                    "reason": "The pattern matches but the code is safe in this context.",
                    "fix": None,
                }
            )
    additional = []
    for p in case.expect.find:
        if (p.file, p.line) in covered:
            continue
        line = files[p.file].splitlines()[p.line - 1].strip()
        additional.append(
            {
                "file": p.file,
                "line": p.line,
                "category": p.category[0],
                "severity": p.min_severity.value,
                "confidence": "high",
                "title": f"Possible {p.category[0].replace('_', ' ')}",
                "description": "User-controlled input reaches this statement without validation.",
                "evidence": line,
                "fix": FIX,
            }
        )
    answer = {
        "summary": "The planted vulnerabilities are real and fixable; the rest are noise.",
        "triage": triage,
        "additional": additional,
    }
    return json.loads(json.dumps({**answer, **overrides}))
