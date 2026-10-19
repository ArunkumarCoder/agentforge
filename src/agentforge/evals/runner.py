"""Run an agent's golden cases and report pass rate, score, tokens, cost and time.

``run_suite`` is the whole eval harness for now: for each case it builds the
agent's input, runs the agent, scores the output with the suite's deterministic
scorer, and records the result. A case ends as one of:

- ``passed``: the agent ran and every check passed,
- ``failed``: the agent ran but some checks failed,
- ``error``: the agent did not produce an output (or scoring itself crashed).

Reports are plain JSON (``SuiteReport.model_dump_json``) so runs can be saved,
compared and charted later. When a run store is given, every case's run is also saved to it
(``source='eval'``) and the case result carries its ``run_id``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from agentforge.core.agent import BaseAgent
from agentforge.core.schemas import Contract, RunResult
from agentforge.evals.scoring import Check
from agentforge.evals.suites import Suite
from agentforge.storage import RunStore


class CaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class CaseResult(Contract):
    case: str
    status: CaseStatus
    score: float = Field(ge=0, le=1)
    checks: list[Check] = Field(default_factory=list)
    error: str | None = None
    run_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: float = 0.0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


class SuiteReport(Contract):
    agent: str
    provider: str = ""
    model: str = ""
    started_at: datetime
    duration_ms: float
    results: list[CaseResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(r.status is CaseStatus.PASSED for r in self.results)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0

    @property
    def mean_score(self) -> float:
        return sum(r.score for r in self.results) / self.total if self.total else 0.0

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results)

    @property
    def tokens(self) -> int:
        return sum(r.tokens for r in self.results)

    def summary(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "passed": self.passed,
            "total": self.total,
            "pass_rate": round(self.pass_rate, 3),
            "mean_score": round(self.mean_score, 3),
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
        }

    def save(self, folder: Path) -> Path:
        """Write the report as ``<UTC time>-<agent>.json`` in ``folder`` and return its path."""
        folder.mkdir(parents=True, exist_ok=True)
        stamp = self.started_at.strftime("%Y%m%dT%H%M%SZ")
        path = folder / f"{stamp}-{self.agent}.json"
        document = {**self.model_dump(mode="json"), "summary": self.summary()}
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        return path


class UnknownCaseError(ValueError):
    """A requested case name does not exist in the suite."""


async def _run_case(
    suite: Suite, agent: BaseAgent[Any, Any], case: Any, store: RunStore | None = None
) -> CaseResult:
    name: str = case.name
    started = time.perf_counter()

    def done(
        status: CaseStatus,
        score: float = 0.0,
        *,
        checks: list[Check] | None = None,
        error: str | None = None,
        result: RunResult[Any] | None = None,
    ) -> CaseResult:
        usage = result.usage if result else None
        return CaseResult(
            case=name,
            status=status,
            score=score,
            checks=checks or [],
            error=error,
            run_id=result.run_id if result else None,
            input_tokens=usage.input_tokens if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cost_usd=usage.cost_usd if usage else 0.0,
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    try:
        data = suite.make_input(case)
        result = await agent.arun(data)
    except Exception as exc:  # an agent should never raise, but a harness must survive it
        return done(CaseStatus.ERROR, error=f"{type(exc).__name__}: {exc}")
    if store is not None:
        await asyncio.to_thread(
            store.try_save, result, input=data.model_dump(mode="json"), source="eval"
        )
    if not result.ok or result.output is None:
        reason = (
            f"{result.error.kind.value}: {result.error.message}" if result.error else "no output"
        )
        return done(CaseStatus.ERROR, error=reason, result=result)
    try:
        scored = await suite.score_case(case, result.output)
    except Exception as exc:  # a bug in a scorer must not hide the other results
        message = f"scoring failed: {type(exc).__name__}: {exc}"
        return done(CaseStatus.ERROR, error=message, result=result)
    status = CaseStatus.PASSED if scored.passed else CaseStatus.FAILED
    return done(status, scored.score, checks=scored.checks, result=result)


async def run_suite(
    suite: Suite,
    agent: BaseAgent[Any, Any],
    *,
    cases: Sequence[str] | None = None,
    concurrency: int = 1,
    provider: str = "",
    model: str = "",
    store: RunStore | None = None,
) -> SuiteReport:
    """Run ``suite`` against ``agent`` (all cases, or just the named ones) and report.

    With a ``store``, each case's run is saved there too (failures to save are only logged).
    """
    available = {c.name: c for c in suite.load_cases()}
    if cases:
        unknown = [name for name in cases if name not in available]
        if unknown:
            raise UnknownCaseError(
                f"no case named {', '.join(unknown)} for {suite.agent} "
                f"(available: {', '.join(sorted(available))})"
            )
        chosen = [available[name] for name in cases]
    else:
        chosen = list(available.values())

    limit = asyncio.Semaphore(max(concurrency, 1))
    started_at, started = datetime.now(UTC), time.perf_counter()

    async def guarded(case: Any) -> CaseResult:
        async with limit:
            return await _run_case(suite, agent, case, store)

    results = await asyncio.gather(*(guarded(c) for c in chosen))
    return SuiteReport(
        agent=suite.agent,
        provider=provider,
        model=model,
        started_at=started_at,
        duration_ms=(time.perf_counter() - started) * 1000,
        results=list(results),
    )
