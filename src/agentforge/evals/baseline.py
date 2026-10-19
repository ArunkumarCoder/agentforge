"""Baselines: what an agent scored last time, and whether this run is worse.

A baseline is a small JSON file kept in the repository (``docs/evals/baselines/<agent>.json``):
for every golden case, the status, score, tokens and cost of one full run against a named
model. ``compare`` lines a new run up against it and names every case that got worse, so
a prompt change that quietly breaks a case shows up as a failing command, not as a feeling.

Comparison rules, per case present in both runs:

- the status ranks ``passed`` > ``failed`` > ``error``; a lower rank is a regression, a
  higher rank an improvement;
- at the same rank, the score moving by more than ``tolerance`` counts the same way;
- cases only in the new run are ``new``; cases only in the baseline are ``missing``
  (reported, never a failure, because ``--case`` runs a subset).

Totals (pass rate, mean score, tokens, cost) use only the cases both runs have, so a
subset run is compared with like for like.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import Field, ValidationError

from agentforge.core.schemas import Contract
from agentforge.evals.runner import CaseStatus, SuiteReport

DEFAULT_TOLERANCE = 0.05
_RANK = {CaseStatus.PASSED: 2, CaseStatus.FAILED: 1, CaseStatus.ERROR: 0}


class BaselineError(ValueError):
    """A baseline or report file could not be read."""


class CaseBaseline(Contract):
    status: CaseStatus
    score: float = Field(ge=0, le=1)
    tokens: int = 0
    cost_usd: float = 0.0


class Baseline(Contract):
    agent: str
    provider: str = ""
    model: str = ""
    recorded_at: datetime
    cases: dict[str, CaseBaseline]

    @property
    def label(self) -> str:
        who = f"{self.provider}/{self.model}" if self.model else (self.provider or "unknown model")
        return f"{who}, {self.recorded_at:%Y-%m-%d}"

    def save(self, folder: Path) -> Path:
        """Write ``<folder>/<agent>.json`` (stable names, so git shows a real diff)."""
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{self.agent}.json"
        document = self.model_dump(mode="json")
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


def baseline_from_report(report: SuiteReport) -> Baseline:
    return Baseline(
        agent=report.agent,
        provider=report.provider,
        model=report.model,
        recorded_at=report.started_at.astimezone(UTC),
        cases={
            r.case: CaseBaseline(
                status=r.status, score=round(r.score, 4), tokens=r.tokens, cost_usd=r.cost_usd
            )
            for r in report.results
        },
    )


def load_baseline(path: Path) -> Baseline:
    """Read a baseline file, or a saved eval report (``eval run`` writes those to .cache/evals)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BaselineError(f"cannot read {path}: {exc.strerror}") from None
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{path} is not valid JSON: {exc.msg} (line {exc.lineno})") from None
    if not isinstance(document, dict):
        raise BaselineError(f"{path} is not a baseline or an eval report")
    try:
        if "results" in document:
            document.pop("summary", None)
            return baseline_from_report(SuiteReport.model_validate(document))
        return Baseline.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first["loc"])
        raise BaselineError(
            f"{path} is not a baseline or an eval report ({where}: {first['msg']})"
        ) from None


class ChangeKind(StrEnum):
    REGRESSION = "regression"
    IMPROVEMENT = "improvement"


class Change(Contract):
    case: str
    kind: ChangeKind
    before: str  # "passed 1.00"
    after: str  # "failed 0.50"


class Comparison(Contract):
    agent: str
    baseline: str  # the baseline's label
    candidate: str
    changes: list[Change]
    new_cases: list[str]
    missing_cases: list[str]
    compared: int
    pass_rate_before: float
    pass_rate_after: float
    mean_score_before: float
    mean_score_after: float
    tokens_before: int
    tokens_after: int
    cost_before: float
    cost_after: float

    @property
    def regressions(self) -> list[Change]:
        return [c for c in self.changes if c.kind is ChangeKind.REGRESSION]

    @property
    def improvements(self) -> list[Change]:
        return [c for c in self.changes if c.kind is ChangeKind.IMPROVEMENT]

    @property
    def regressed(self) -> bool:
        return bool(self.regressions)


def _describe(case: CaseBaseline) -> str:
    return f"{case.status.value} {case.score:.2f}"


def compare(old: Baseline, new: Baseline, *, tolerance: float = DEFAULT_TOLERANCE) -> Comparison:
    """How ``new`` differs from ``old``. Both must be for the same agent."""
    if old.agent != new.agent:
        raise BaselineError(f"cannot compare '{old.agent}' with '{new.agent}'")
    common = [name for name in new.cases if name in old.cases]
    changes: list[Change] = []
    for name in sorted(common):
        before, after = old.cases[name], new.cases[name]
        delta_rank = _RANK[after.status] - _RANK[before.status]
        delta_score = after.score - before.score
        if delta_rank != 0:
            kind = ChangeKind.IMPROVEMENT if delta_rank > 0 else ChangeKind.REGRESSION
        elif abs(delta_score) > tolerance:
            kind = ChangeKind.IMPROVEMENT if delta_score > 0 else ChangeKind.REGRESSION
        else:
            continue
        changes.append(
            Change(case=name, kind=kind, before=_describe(before), after=_describe(after))
        )

    def rate(cases: dict[str, CaseBaseline]) -> float:
        done = [cases[n] for n in common]
        return sum(c.status is CaseStatus.PASSED for c in done) / len(done) if done else 0.0

    def mean(cases: dict[str, CaseBaseline]) -> float:
        done = [cases[n].score for n in common]
        return sum(done) / len(done) if done else 0.0

    return Comparison(
        agent=new.agent,
        baseline=old.label,
        candidate=new.label,
        changes=changes,
        new_cases=sorted(set(new.cases) - set(old.cases)),
        missing_cases=sorted(set(old.cases) - set(new.cases)),
        compared=len(common),
        pass_rate_before=rate(old.cases),
        pass_rate_after=rate(new.cases),
        mean_score_before=mean(old.cases),
        mean_score_after=mean(new.cases),
        tokens_before=sum(old.cases[n].tokens for n in common),
        tokens_after=sum(new.cases[n].tokens for n in common),
        cost_before=sum(old.cases[n].cost_usd for n in common),
        cost_after=sum(new.cases[n].cost_usd for n in common),
    )
