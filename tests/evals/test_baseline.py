"""Baselines, comparisons and the Markdown summary: pure functions, no agents needed."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentforge.evals import (
    BaselineError,
    CaseStatus,
    baseline_from_report,
    compare,
    load_baseline,
    render_markdown,
)
from agentforge.evals.baseline import Baseline, CaseBaseline, ChangeKind
from agentforge.evals.runner import CaseResult, SuiteReport
from agentforge.evals.scoring import Check

WHEN = datetime(2026, 10, 20, 9, 30, tzinfo=UTC)


def case(status: CaseStatus, score: float, tokens: int = 100, cost: float = 0.01) -> CaseBaseline:
    return CaseBaseline(status=status, score=score, tokens=tokens, cost_usd=cost)


P, F, E = CaseStatus.PASSED, CaseStatus.FAILED, CaseStatus.ERROR


def base(cases: dict[str, CaseBaseline], agent: str = "echo", model: str = "m1") -> Baseline:
    return Baseline(agent=agent, provider="openai", model=model, recorded_at=WHEN, cases=cases)


# --------------------------------------------------------------------------- compare


def test_no_change_is_no_changes() -> None:
    same = {"a": case(P, 1.0), "b": case(F, 0.5)}
    result = compare(base(same), base(dict(same)))
    assert result.changes == []
    assert not result.regressed
    assert (result.compared, result.new_cases, result.missing_cases) == (2, [], [])


def test_a_status_drop_is_a_regression_and_a_rise_an_improvement() -> None:
    old = base({"a": case(P, 1.0), "b": case(F, 0.5), "c": case(E, 0.0), "d": case(P, 1.0)})
    new = base({"a": case(F, 0.5), "b": case(P, 1.0), "c": case(F, 0.4), "d": case(E, 0.0)})
    result = compare(old, new)
    kinds = {c.case: c.kind for c in result.changes}
    assert kinds == {
        "a": ChangeKind.REGRESSION,
        "b": ChangeKind.IMPROVEMENT,
        "c": ChangeKind.IMPROVEMENT,  # error -> failed
        "d": ChangeKind.REGRESSION,  # passed -> error
    }
    assert [c.case for c in result.regressions] == ["a", "d"]
    assert result.regressed
    a = next(c for c in result.changes if c.case == "a")
    assert (a.before, a.after) == ("passed 1.00", "failed 0.50")


def test_score_moves_inside_the_tolerance_are_ignored() -> None:
    old = base({"a": case(F, 0.50), "b": case(F, 0.50), "c": case(F, 0.50)})
    new = base({"a": case(F, 0.46), "b": case(F, 0.30), "c": case(F, 0.80)})
    result = compare(old, new, tolerance=0.05)
    assert {c.case: c.kind for c in result.changes} == {
        "b": ChangeKind.REGRESSION,
        "c": ChangeKind.IMPROVEMENT,
    }
    assert compare(old, new, tolerance=0.5).changes == []


def test_new_and_missing_cases_are_reported_but_never_regressions() -> None:
    old = base({"a": case(P, 1.0), "gone": case(P, 1.0)})
    new = base({"a": case(P, 1.0), "fresh": case(F, 0.0)})
    result = compare(old, new)
    assert result.new_cases == ["fresh"]
    assert result.missing_cases == ["gone"]
    assert not result.regressed
    assert result.compared == 1


def test_totals_use_only_the_cases_both_runs_have() -> None:
    old = base({"a": case(P, 1.0, 100, 0.10), "b": case(F, 0.0, 200, 0.20), "x": case(P, 1.0)})
    new = base({"a": case(P, 1.0, 150, 0.30), "b": case(P, 1.0, 50, 0.05)})
    r = compare(old, new)
    assert (r.pass_rate_before, r.pass_rate_after) == (0.5, 1.0)
    assert (r.mean_score_before, r.mean_score_after) == (0.5, 1.0)
    assert (r.tokens_before, r.tokens_after) == (300, 200)
    assert r.cost_before == pytest.approx(0.30)
    assert r.cost_after == pytest.approx(0.35)


def test_nothing_in_common_does_not_divide_by_zero() -> None:
    r = compare(base({"a": case(P, 1.0)}), base({"b": case(P, 1.0)}))
    assert (r.compared, r.pass_rate_before, r.mean_score_after) == (0, 0.0, 0.0)


def test_different_agents_cannot_be_compared() -> None:
    with pytest.raises(BaselineError, match="cannot compare 'echo' with 'other'"):
        compare(base({}), base({}, agent="other"))


def test_labels_name_the_model_and_date() -> None:
    assert base({}).label == "openai/m1, 2026-10-20"
    anonymous = Baseline(agent="a", recorded_at=WHEN, cases={})
    assert anonymous.label == "unknown model, 2026-10-20"
    assert compare(base({}), base({}, model="m2")).candidate == "openai/m2, 2026-10-20"


# --------------------------------------------------------------------------- files


def report() -> SuiteReport:
    ok = CaseResult(case="a", status=P, score=1.0, input_tokens=60, output_tokens=40, cost_usd=0.02)
    bad = CaseResult(
        case="b",
        status=F,
        score=0.5,
        checks=[
            Check(name="text", passed=False, detail="wrong | text"),
            Check(name="n", passed=True),
        ],
    )
    err = CaseResult(case="c", status=E, score=0.0, error="timeout: too slow")
    return SuiteReport(
        agent="echo",
        provider="openai",
        model="m1",
        started_at=WHEN,
        duration_ms=5,
        results=[ok, bad, err],
    )


def test_a_baseline_comes_from_a_report() -> None:
    b = baseline_from_report(report())
    assert b.cases["a"] == CaseBaseline(status=P, score=1.0, tokens=100, cost_usd=0.02)
    assert (b.agent, b.provider, b.model, b.recorded_at) == ("echo", "openai", "m1", WHEN)
    assert set(b.cases) == {"a", "b", "c"}


def test_baselines_are_saved_with_stable_names_and_read_back(tmp_path: Path) -> None:
    b = baseline_from_report(report())
    path = b.save(tmp_path / "docs" / "baselines")
    assert path.name == "echo.json"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert list(json.loads(text)["cases"]) == ["a", "b", "c"]  # sorted keys: a clean git diff
    assert load_baseline(path) == b


def test_saved_eval_reports_load_as_baselines(tmp_path: Path) -> None:
    saved = report().save(tmp_path)  # what `eval run` writes to .cache/evals
    loaded = load_baseline(saved)
    assert loaded == baseline_from_report(report())


def test_bad_files_are_explained(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="cannot read"):
        load_baseline(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    with pytest.raises(BaselineError, match="not valid JSON"):
        load_baseline(broken)
    wrong = tmp_path / "list.json"
    wrong.write_text("[1]", encoding="utf-8")
    with pytest.raises(BaselineError, match="not a baseline or an eval report"):
        load_baseline(wrong)
    odd = tmp_path / "odd.json"
    odd.write_text('{"agent": "x"}', encoding="utf-8")
    with pytest.raises(BaselineError, match=r"recorded_at"):
        load_baseline(odd)


# --------------------------------------------------------------------------- markdown


def test_markdown_summary_with_and_without_a_baseline() -> None:
    r = report()
    plain = render_markdown([r])
    assert plain.startswith("# Eval results")
    assert "Model: `openai/m1`" in plain
    assert "| echo | 1/3 | 33% | 0.50 | 100 | $0.0200 | none |" in plain
    assert "**1/3 cases passed**" in plain
    assert "| b | FAIL | 0.50 | 0 | $0.0000 | text (wrong \\| text) |" in plain  # pipes escaped
    assert "| c | ERROR | 0.00 | 0 | $0.0000 | timeout: too slow |" in plain

    old = base({"a": case(P, 1.0), "b": case(P, 1.0), "c": case(P, 1.0), "z": case(P, 1.0)})
    cmp = compare(old, baseline_from_report(r))
    text = render_markdown([r], {"echo": cmp})
    assert "2 regression(s)" in text
    assert "**regression** `b`: passed 1.00 → failed 0.50" in text
    assert "- not run: z" in text
    clean = compare(baseline_from_report(r), baseline_from_report(r))
    assert "no regressions" in render_markdown([r], {"echo": clean})
    assert "- no case changed" in render_markdown([r], {"echo": clean})


def test_markdown_for_no_reports_is_still_valid() -> None:
    text = render_markdown([])
    assert "0/0 cases passed" in text
