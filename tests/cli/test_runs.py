"""`agentforge runs ...` and run saving from `agents run`, against a temporary SQLite file."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentforge.cli.main import EXIT_RUN_FAILED, EXIT_USAGE, app
from agentforge.config import get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


@pytest.fixture
def history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "runs.db"
    monkeypatch.setenv("AGENTFORGE_STORAGE__URL", f"sqlite:///{path}")
    return path


def run_echo(text: str) -> str:
    result = runner.invoke(
        app, ["agents", "run", "echo", "-d", json.dumps({"text": text}), "--json"]
    )
    return str(json.loads(result.stdout)["run_id"])


def test_agents_run_saves_the_run(history: Path) -> None:
    run_id = run_echo("hello")
    assert history.exists()
    shown = json.loads(runner.invoke(app, ["runs", "show", run_id, "--json"]).stdout)
    assert shown["input"] == {"text": "hello"}
    assert shown["output"] == {"text": "hello", "length": 5}
    assert shown["source"] == "cli"


def test_a_failed_run_is_saved_and_filterable(history: Path) -> None:
    run_echo("fine")
    bad = runner.invoke(app, ["agents", "run", "echo", "-d", '{"text": ""}', "--json"])
    assert bad.exit_code == EXIT_RUN_FAILED
    failed = json.loads(runner.invoke(app, ["runs", "list", "--status", "failed", "--json"]).stdout)
    assert failed["total"] == 1
    assert failed["runs"][0]["error_kind"] == "input_validation"


def test_without_a_database_nothing_is_saved_and_runs_still_work() -> None:
    result = runner.invoke(app, ["agents", "run", "echo", "-d", '{"text": "hi"}'])
    assert result.exit_code == 0
    assert not list(Path(".").glob("*.db"))


def test_a_broken_database_is_a_warning_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_STORAGE__URL", "postgresql://u:hunter2@127.0.0.1:1/none")
    result = runner.invoke(app, ["agents", "run", "echo", "-d", '{"text": "hi"}'])
    assert result.exit_code == 0
    assert "runs will not be saved" in " ".join(result.stderr.split())
    assert "hunter2" not in result.stderr


def test_list_table(history: Path) -> None:
    run_echo("a")
    run_echo("b")
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == 0
    assert "2 of 2 run(s)" in result.stdout
    assert "echo" in result.stdout


def test_list_filters_and_limit(history: Path) -> None:
    for t in "abc":
        run_echo(t)
    data = json.loads(runner.invoke(app, ["runs", "list", "-n", "2", "--json"]).stdout)
    assert (data["total"], len(data["runs"])) == (3, 2)
    other = json.loads(runner.invoke(app, ["runs", "list", "-a", "nope", "--json"]).stdout)
    assert other["total"] == 0
    bad = runner.invoke(app, ["runs", "list", "--status", "weird"])
    assert bad.exit_code == EXIT_USAGE


def test_show_pretty_and_unknown(history: Path) -> None:
    run_id = run_echo("hi")
    result = runner.invoke(app, ["runs", "show", run_id])
    assert result.exit_code == 0
    assert run_id in result.stdout
    assert "succeeded" in result.stdout
    assert "Input" in result.stdout and "Output" in result.stdout
    missing = runner.invoke(app, ["runs", "show", "nope"])
    assert missing.exit_code == EXIT_USAGE
    assert "no run with id 'nope'" in missing.stderr


def test_show_a_failed_run_prints_the_error(history: Path) -> None:
    runner.invoke(app, ["agents", "run", "echo", "-d", '{"text": ""}'])
    run_id = json.loads(runner.invoke(app, ["runs", "list", "--json"]).stdout)["runs"][0]["run_id"]
    result = runner.invoke(app, ["runs", "show", run_id])
    assert "input_validation" in result.stdout
    assert "failed" in result.stdout


def test_show_says_when_inputs_are_not_stored(
    history: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTFORGE_STORAGE__STORE_INPUTS", "false")
    run_id = run_echo("private")
    result = runner.invoke(app, ["runs", "show", run_id])
    assert "input was not stored" in result.stdout
    assert "private" in result.stdout  # only as the output


def test_stats(history: Path) -> None:
    run_echo("a")
    run_echo("b")
    runner.invoke(app, ["agents", "run", "echo", "-d", '{"text": ""}'])
    stats = json.loads(runner.invoke(app, ["runs", "stats", "--json"]).stdout)
    assert (stats[0]["agent"], stats[0]["runs"], stats[0]["failed"]) == ("echo", 3, 1)
    table = runner.invoke(app, ["runs", "stats", "--days", "1"])
    assert table.exit_code == 0
    assert "67%" in table.stdout
    assert "3 run(s)" in table.stdout


def test_prune(history: Path) -> None:
    run_echo("a")
    kept = runner.invoke(app, ["runs", "prune", "--days", "1", "--yes"])
    assert "Deleted 0 run(s)." in kept.stdout
    declined = runner.invoke(app, ["runs", "prune", "--days", "0"], input="n\n")
    assert declined.exit_code == EXIT_USAGE
    assert json.loads(runner.invoke(app, ["runs", "list", "--json"]).stdout)["total"] == 1
    confirmed = runner.invoke(app, ["runs", "prune", "--days", "0"], input="y\n")
    assert "Deleted 1 run(s)." in confirmed.stdout
    assert json.loads(runner.invoke(app, ["runs", "list", "--json"]).stdout)["total"] == 0


@pytest.mark.parametrize("command", [["list"], ["show", "x"], ["stats"], ["prune", "--days", "1"]])
def test_commands_say_how_to_turn_history_on(command: list[str]) -> None:
    result = runner.invoke(app, ["runs", *command])
    assert result.exit_code == EXIT_USAGE
    assert "AGENTFORGE_STORAGE__URL" in " ".join(result.stderr.split())


def test_commands_report_an_unreachable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_STORAGE__URL", "postgresql://u:hunter2@127.0.0.1:1/none")
    result = runner.invoke(app, ["runs", "list"])
    assert result.exit_code == EXIT_RUN_FAILED
    assert "cannot open the run store" in result.stderr
    assert "hunter2" not in result.stderr


def test_config_show_masks_the_database_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_STORAGE__URL", "postgresql://app:hunter2@db.internal/agentforge")
    result = runner.invoke(app, ["config", "show"])
    data = json.loads(result.stdout)
    assert data["storage"]["url"] == "postgresql+psycopg://app:***@db.internal/agentforge"
    assert "hunter2" not in result.stdout
    assert data["storage"]["store_inputs"] is True


def test_config_show_when_history_is_off() -> None:
    data = json.loads(runner.invoke(app, ["config", "show"]).stdout)
    assert data["storage"]["url"] == "off"
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setenv("AGENTFORGE_STORAGE__URL", "mysql://x/y")
        get_settings.cache_clear()
        data = json.loads(runner.invoke(app, ["config", "show"]).stdout)
        assert data["storage"]["url"] == "set (invalid)"
    finally:
        monkey.undo()
        get_settings.cache_clear()


def test_serve_refuses_to_start_with_a_bad_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTFORGE_STORAGE__URL", "postgresql://u:p@127.0.0.1:1/none")
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("must not start"))
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == EXIT_RUN_FAILED
    assert "cannot open the run store" in result.stderr


def test_serve_says_whether_history_is_on(history: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: None)
    assert "saving runs" in runner.invoke(app, ["serve"]).stderr
    monkeypatch.delenv("AGENTFORGE_STORAGE__URL")
    get_settings.cache_clear()
    assert "run history is off" in runner.invoke(app, ["serve"]).stderr
