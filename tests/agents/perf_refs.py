"""A perfect analysis for each golden case, derived from the case's answer key.

The real tools run over the case (the static scan, and cProfile when the case has a profile
script). Every candidate that answers a planted slowdown is confirmed, the others are
dismissed, and each planted slowdown no tool reported becomes an ``additional`` finding that
quotes its line. The result scores full marks: tests use it as the model's ideal answer and
to prove the cases measure what they claim.
"""

import json
from typing import Any

from agentforge.agents.performance import number_candidates
from agentforge.agents.performance.scoring import EvalCase, covers
from agentforge.tools import perf_scan
from agentforge.tools.perf_scan import Candidate, Hotspot

FIX = "Use a set or dict for lookups, join the parts once, and batch the queries."


async def evidence_for(
    case: EvalCase,
) -> tuple[dict[str, tuple[Candidate, str]], list[Hotspot]]:
    files: dict[str, str] = case.input["files"]
    script = case.input.get("profile_script")
    hotspots = await perf_scan.profile(files, script) if script else []
    return number_candidates(perf_scan.scan_python(files), hotspots), hotspots


async def reference_answer(case: EvalCase, **overrides: Any) -> dict[str, Any]:
    files: dict[str, str] = case.input["files"]
    candidates, _ = await evidence_for(case)
    triage = []
    covered: set[tuple[str, int]] = set()
    for cid, (c, _source) in candidates.items():
        hit = next(
            (p for p in case.expect.find if covers(p, c.file, c.line, c.function, c.kind)), None
        )
        if hit:
            covered.add((hit.file, hit.line))
            triage.append(
                {
                    "finding_id": cid,
                    "verdict": "confirmed",
                    "impact": hit.min_impact.value,
                    "reason": f"{c.title}; the cost grows with the size of the input.",
                    "fix": FIX,
                }
            )
        else:
            triage.append(
                {
                    "finding_id": cid,
                    "verdict": "false_positive",
                    "impact": "low",
                    "reason": "The pattern is present but the cost is negligible here.",
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
                "kind": p.kind[0],
                "impact": p.min_impact.value,
                "confidence": "high",
                "title": f"Slow code: {p.kind[0].replace('_', ' ')}",
                "description": "The work grows much faster than the input and should be reduced.",
                "evidence": line,
                "fix": FIX,
            }
        )
    answer = {
        "summary": "The planted slowdowns are real and fixable; the rest are harmless.",
        "triage": triage,
        "additional": additional,
    }
    return json.loads(json.dumps({**answer, **overrides}))
