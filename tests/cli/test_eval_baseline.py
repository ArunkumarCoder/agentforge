"""`agentforge eval` with baselines, `eval compare`, the Markdown summary and run history."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentforge.cli.main import EXIT_RUN_FAILED, EXIT_USAGE, app
from agentforge.config import get_settings
from tests.cli import test_cli

runner = CliRunner()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


def use_suite(monkeypatch: pytest.MonkeyPatch, *, expect_upper: bool) -> None:
    test_cli.TestEval.echo_suite(monkeypatch, expect_upper=expect_upper)


def real_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite is stubbed, so only the *name* of the provider matters here."""
    monkeypatch.setenv("AGENTFORGE_LLM__PROVIDER", "openai")
    monkeypatch.setenv("AGENTFORGE_LLM__MODEL", "gpt-test")
    get_settings.cache_clear()


def run(*args: str) -> object:
    return runner.invoke(app, ["eval", "run", "echo", "--no-save", *args])


def test_save_baseline_writes_one_file_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    real_provider(monkeypatch)
    result = runner.invoke(app, ["eval", "run", "echo", "--no-save", "--save-baseline"])
    assert result.exit_code == 0, result.output
    path = Path("docs/evals/baselines/echo.json")
    data = json.loads(path.read_text())
    assert (data["agent"], data["provider"], data["model"]) == ("echo", "openai", "gpt-test")
    assert {k: v["status"] for k, v in data["cases"].items()} == {"a": "passed", "b": "passed"}
    assert "baseline written" in result.stderr


def test_a_baseline_needs_a_full_run_and_a_real_model(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    fake = runner.invoke(app, ["eval", "run", "echo", "--allow-fake", "--save-baseline"])
    assert fake.exit_code == EXIT_USAGE
    assert "real model" in " ".join(fake.stderr.split())
    real_provider(monkeypatch)
    subset = runner.invoke(app, ["eval", "run", "echo", "-c", "a", "--save-baseline"])
    assert subset.exit_code == EXIT_USAGE
    assert "full run" in " ".join(subset.stderr.split())
    assert not Path("docs").exists()


def test_a_run_is_compared_with_its_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    real_provider(monkeypatch)
    runner.invoke(app, ["eval", "run", "echo", "--no-save", "--save-baseline"])
    same = runner.invoke(app, ["eval", "run", "echo", "--no-save"])
    assert same.exit_code == 0
    assert "vs baseline" in same.stdout
    assert "no case changed" in same.stdout
    assert "pass rate 100% → 100%" in same.stdout


def test_a_regression_exits_1_even_above_the_pass_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    real_provider(monkeypatch)
    runner.invoke(app, ["eval", "run", "echo", "--no-save", "--save-baseline"])
    use_suite(monkeypatch, expect_upper=True)  # now every case fails
    worse = runner.invoke(app, ["eval", "run", "echo", "--no-save", "--min-pass-rate", "0"])
    assert worse.exit_code == EXIT_RUN_FAILED
    assert "regression" in worse.stdout
    assert "passed 1.00 → failed 0.00" in worse.stdout
    assert "Regressed against the baseline: echo" in worse.stderr


def test_an_improvement_is_shown_and_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=True)
    real_provider(monkeypatch)
    runner.invoke(
        app, ["eval", "run", "echo", "--no-save", "--save-baseline", "--min-pass-rate", "0"]
    )
    use_suite(monkeypatch, expect_upper=False)
    better = runner.invoke(app, ["eval", "run", "echo", "--no-save"])
    assert better.exit_code == 0
    assert "improvement" in better.stdout


def test_a_subset_run_compares_only_what_it_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    real_provider(monkeypatch)
    runner.invoke(app, ["eval", "run", "echo", "--no-save", "--save-baseline"])
    subset = runner.invoke(app, ["eval", "run", "echo", "-c", "a", "--no-save"])
    assert subset.exit_code == 0
    assert "on 1 case(s)" in subset.stdout
    assert "not run: b" in subset.stdout


def test_a_custom_baseline_folder_and_a_broken_baseline(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    real_provider(monkeypatch)
    runner.invoke(
        app, ["eval", "run", "echo", "--no-save", "--save-baseline", "--baseline-dir", "mine"]
    )
    assert Path("mine/echo.json").is_file()
    Path("mine/echo.json").write_text("{nope", encoding="utf-8")
    result = runner.invoke(app, ["eval", "run", "echo", "--no-save", "--baseline-dir", "mine"])
    assert result.exit_code == 0  # a broken baseline is a warning, not a failure
    assert "ignoring the baseline" in " ".join(result.stderr.split())


def test_json_output_includes_the_comparison(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    real_provider(monkeypatch)
    runner.invoke(app, ["eval", "run", "echo", "--no-save", "--save-baseline"])
    result = runner.invoke(app, ["eval", "run", "echo", "--no-save", "--json"])
    (summary,) = json.loads(result.stdout)
    assert summary["baseline"]["compared"] == 2
    assert summary["baseline"]["changes"] == []
    use_suite(monkeypatch, expect_upper=False)
    fresh = runner.invoke(
        app, ["eval", "run", "echo", "--no-save", "--json", "--baseline-dir", "nowhere"]
    )
    assert json.loads(fresh.stdout)[0]["baseline"] is None


def test_markdown_summary_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    real_provider(monkeypatch)
    result = runner.invoke(
        app, ["eval", "run", "echo", "--no-save", "--markdown", "docs/evals/results.md"]
    )
    assert result.exit_code == 0
    text = Path("docs/evals/results.md").read_text(encoding="utf-8")
    assert "# Eval results" in text
    assert "| echo | 2/2 | 100% |" in text


def test_eval_runs_are_saved_to_the_run_history(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    monkeypatch.setenv("AGENTFORGE_STORAGE__URL", "sqlite:///runs.db")
    get_settings.cache_clear()
    assert runner.invoke(app, ["eval", "run", "echo", "--allow-fake", "--no-save"]).exit_code == 0
    page = json.loads(runner.invoke(app, ["runs", "list", "--json"]).stdout)
    assert page["total"] == 2
    assert {r["source"] for r in page["runs"]} == {"eval"}


def test_no_history_skips_the_run_store(monkeypatch: pytest.MonkeyPatch) -> None:
    use_suite(monkeypatch, expect_upper=False)
    monkeypatch.setenv("AGENTFORGE_STORAGE__URL", "sqlite:///runs.db")
    get_settings.cache_clear()
    result = runner.invoke(
        app, ["eval", "run", "echo", "--allow-fake", "--no-save", "--no-history"]
    )
    assert result.exit_code == 0
    assert not Path("runs.db").exists()


# --------------------------------------------------------------------------- eval compare


def write(path: Path, statuses: dict[str, str], model: str = "m") -> Path:
    cases = {
        n: {"status": s, "score": 1.0 if s == "passed" else 0.0, "tokens": 10, "cost_usd": 0.01}
        for n, s in statuses.items()
    }
    document = {
        "agent": "echo",
        "provider": "openai",
        "model": model,
        "recorded_at": "2026-10-20T09:00:00Z",
        "cases": cases,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_compare_two_files(tmp_path: Path) -> None:
    old = write(tmp_path / "old.json", {"a": "passed", "b": "passed"}, "m1")
    new = write(tmp_path / "new.json", {"a": "passed", "b": "failed"}, "m2")
    result = runner.invoke(app, ["eval", "compare", str(old), str(new)])
    assert result.exit_code == EXIT_RUN_FAILED
    assert "regression" in result.stdout
    assert "b: passed 1.00 → failed 0.00" in result.stdout
    assert runner.invoke(app, ["eval", "compare", str(new), str(old)]).exit_code == 0
    as_json = runner.invoke(app, ["eval", "compare", str(old), str(new), "--json"])
    assert json.loads(as_json.stdout)["changes"][0]["case"] == "b"


def test_compare_accepts_saved_reports_and_explains_bad_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    use_suite(monkeypatch, expect_upper=False)
    runner.invoke(app, ["eval", "run", "echo", "--allow-fake", "--out", "reports"])
    (report,) = Path("reports").glob("*-echo.json")
    assert runner.invoke(app, ["eval", "compare", str(report), str(report)]).exit_code == 0
    missing = runner.invoke(app, ["eval", "compare", "nope.json", str(report)])
    assert missing.exit_code == EXIT_USAGE
    assert "cannot read nope.json" in " ".join(missing.stderr.split())
    other = write(tmp_path / "other.json", {"a": "passed"})
    document = json.loads(other.read_text())
    document["agent"] = "different"
    other.write_text(json.dumps(document), encoding="utf-8")
    mismatch = runner.invoke(app, ["eval", "compare", str(report), str(other)])
    assert mismatch.exit_code == EXIT_USAGE
    assert "cannot compare" in " ".join(mismatch.stderr.split())
