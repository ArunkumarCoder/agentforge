"""A Markdown summary of eval runs, for ``docs/evals/results.md`` and pull requests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentforge.evals.baseline import Comparison
from agentforge.evals.runner import CaseStatus, SuiteReport

_MARK = {CaseStatus.PASSED: "pass", CaseStatus.FAILED: "FAIL", CaseStatus.ERROR: "ERROR"}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(
    reports: Sequence[SuiteReport], comparisons: Mapping[str, Comparison] | None = None
) -> str:
    """One table of totals, then one section per agent with its cases and any regressions."""
    comparisons = comparisons or {}
    lines = ["# Eval results", ""]
    if reports:
        first = reports[0]
        model = f"{first.provider}/{first.model}" if first.model else first.provider or "unknown"
        lines += [f"Model: `{model}`. Run at {first.started_at:%Y-%m-%d %H:%M} UTC.", ""]
    lines += [
        "| Agent | Passed | Pass rate | Mean score | Tokens | Cost | Baseline |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        cmp = comparisons.get(r.agent)
        if cmp is None:
            versus = "none"
        elif cmp.regressed:
            versus = f"{len(cmp.regressions)} regression(s)"
        else:
            versus = "no regressions"
        lines.append(
            f"| {r.agent} | {r.passed}/{r.total} | {r.pass_rate:.0%} | {r.mean_score:.2f} "
            f"| {r.tokens} | ${r.cost_usd:.4f} | {versus} |"
        )
    total, passed = sum(r.total for r in reports), sum(r.passed for r in reports)
    cost = sum(r.cost_usd for r in reports)
    lines += [
        "",
        f"**{passed}/{total} cases passed**, {sum(r.tokens for r in reports)} tokens, ${cost:.4f}.",
    ]
    for r in reports:
        lines += ["", f"## {r.agent}", "", "| Case | Result | Score | Tokens | Cost | Problems |"]
        lines.append("|---|---|---|---|---|---|")
        for c in r.results:
            problems = c.error or "; ".join(f"{k.name} ({k.detail})" for k in c.failed_checks())
            lines.append(
                f"| {c.case} | {_MARK[c.status]} | {c.score:.2f} | {c.tokens} "
                f"| ${c.cost_usd:.4f} | {_cell(problems)} |"
            )
        cmp = comparisons.get(r.agent)
        if cmp is not None:
            lines += ["", f"Against the baseline ({cmp.baseline}):", ""]
            if not cmp.changes:
                lines.append("- no case changed")
            for ch in cmp.changes:
                lines.append(f"- **{ch.kind.value}** `{ch.case}`: {ch.before} → {ch.after}")
            if cmp.new_cases:
                lines.append(f"- new cases: {', '.join(cmp.new_cases)}")
            if cmp.missing_cases:
                lines.append(f"- not run: {', '.join(cmp.missing_cases)}")
    return "\n".join(lines) + "\n"
