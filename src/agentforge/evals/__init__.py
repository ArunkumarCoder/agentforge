"""Evaluation harness: run an agent's golden cases and record how it did."""

from agentforge.evals.baseline import (
    Baseline,
    BaselineError,
    Change,
    Comparison,
    baseline_from_report,
    compare,
    load_baseline,
)
from agentforge.evals.report import render_markdown
from agentforge.evals.runner import (
    CaseResult,
    CaseStatus,
    SuiteReport,
    UnknownCaseError,
    run_suite,
)
from agentforge.evals.scoring import CaseScore, Check
from agentforge.evals.suites import Suite, all_suites

__all__ = [
    "Baseline",
    "BaselineError",
    "CaseResult",
    "CaseScore",
    "CaseStatus",
    "Change",
    "Check",
    "Comparison",
    "Suite",
    "SuiteReport",
    "UnknownCaseError",
    "all_suites",
    "baseline_from_report",
    "compare",
    "load_baseline",
    "render_markdown",
    "run_suite",
]
