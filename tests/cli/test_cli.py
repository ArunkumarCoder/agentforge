"""CLI tests with Typer's CliRunner: real commands, offline (fake provider, echo agent)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentforge import __version__
from agentforge.cli.main import EXIT_RUN_FAILED, EXIT_USAGE, app
from agentforge.config import get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # no stray .env files
    get_settings.cache_clear()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"agentforge {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "agents" in result.output
    assert "llm" in result.output


class TestAgents:
    def test_list_table(self) -> None:
        result = runner.invoke(app, ["agents", "list"])
        assert result.exit_code == 0
        assert "echo" in result.stdout
        assert "demo, no-llm" in result.stdout

    def test_list_json(self) -> None:
        result = runner.invoke(app, ["agents", "list", "--json"])
        data = json.loads(result.stdout)
        echo = next(a for a in data if a["name"] == "echo")
        assert echo["tags"] == ["demo", "no-llm"]

    def test_show(self) -> None:
        result = runner.invoke(app, ["agents", "show", "echo"])
        assert result.exit_code == 0
        assert "Returns the text it is given" in result.stdout
        assert "Input schema" in result.stdout
        assert '"upper"' in result.stdout

    def test_show_unknown(self) -> None:
        result = runner.invoke(app, ["agents", "show", "nope"])
        assert result.exit_code == EXIT_USAGE
        assert "no agent named 'nope'" in result.stderr


class TestRun:
    def test_run_with_data(self) -> None:
        result = runner.invoke(
            app, ["agents", "run", "echo", "-d", '{"text": "hi", "upper": true}']
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {"text": "HI", "length": 2}
        assert "echo succeeded" in result.stderr

    def test_run_json_prints_full_result(self) -> None:
        result = runner.invoke(app, ["agents", "run", "echo", "-d", '{"text": "hi"}', "--json"])
        data = json.loads(result.stdout)
        assert data["status"] == "succeeded"
        assert data["output"] == {"text": "hi", "length": 2}
        assert data["agent"] == "echo"
        assert "run_id" in data

    def test_run_from_file(self, tmp_path: Path) -> None:
        payload = tmp_path / "in.json"
        payload.write_text('{"text": "from file"}', encoding="utf-8")
        result = runner.invoke(app, ["agents", "run", "echo", "--input", str(payload)])
        assert result.exit_code == 0
        assert json.loads(result.stdout)["text"] == "from file"

    def test_run_from_stdin(self) -> None:
        result = runner.invoke(app, ["agents", "run", "echo", "-i", "-"], input='{"text": "piped"}')
        assert result.exit_code == 0
        assert json.loads(result.stdout)["text"] == "piped"

    def test_failed_run_exits_1_and_keeps_brackets(self) -> None:
        result = runner.invoke(app, ["agents", "run", "echo", "-d", "{}"])
        assert result.exit_code == EXIT_RUN_FAILED
        assert "echo failed (input_validation)" in result.stderr
        assert "[type=missing" in result.stderr  # Rich markup must not swallow it
        assert result.stdout == ""

    def test_failed_run_as_json(self) -> None:
        result = runner.invoke(app, ["agents", "run", "echo", "-d", "{}", "--json"])
        assert result.exit_code == EXIT_RUN_FAILED
        assert json.loads(result.stdout)["error"]["kind"] == "input_validation"

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["-d", "{not json"], "--data is not valid JSON"),
            (["-d", "[1, 2]"], "must be a JSON object"),
            (["-d", "{}", "-i", "x.json"], "either --data or --input"),
            (["-i", "missing.json"], "cannot read missing.json"),
        ],
    )
    def test_bad_input(self, args: list[str], message: str) -> None:
        result = runner.invoke(app, ["agents", "run", "echo", *args])
        assert result.exit_code == EXIT_USAGE
        assert message in result.stderr

    def test_unknown_agent(self) -> None:
        result = runner.invoke(app, ["agents", "run", "nope"])
        assert result.exit_code == EXIT_USAGE
        assert "available: bug-fix, code-review, echo" in result.stderr

    def test_default_input_is_empty_object(self) -> None:
        result = runner.invoke(app, ["agents", "run", "echo"])
        assert result.exit_code == EXIT_RUN_FAILED  # echo requires "text"


class TestLogging:
    def test_quiet_by_default(self) -> None:
        result = runner.invoke(app, ["agents", "run", "echo", "-d", '{"text": "x"}'])
        assert "agent.run.started" not in result.stderr

    def test_verbose_and_json_logs(self) -> None:
        result = runner.invoke(
            app, ["-v", "--json-logs", "agents", "run", "echo", "-d", '{"text": "x"}']
        )
        log_lines = [
            json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")
        ]
        assert [e["event"] for e in log_lines] == ["agent.run.started", "agent.run.succeeded"]

    def test_explicit_level_and_invalid_level(self) -> None:
        result = runner.invoke(
            app, ["--log-level", "debug", "agents", "run", "echo", "-d", '{"text": "x"}']
        )
        assert "agent.run.started" in result.stderr
        result = runner.invoke(app, ["--log-level", "LOUD", "agents", "list"])
        assert result.exit_code == EXIT_USAGE
        assert "log level must be one of" in result.stderr


class TestLlmAndConfig:
    def test_llm_ping_with_fake_provider(self) -> None:
        result = runner.invoke(app, ["llm", "ping", "-p", "hello there"])
        assert result.exit_code == 0
        assert result.stdout.strip() == "[fake] hello there"
        assert "fake/fake-model" in result.stderr

    def test_llm_ping_reports_provider_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTFORGE_LLM__PROVIDER", "anthropic")
        result = runner.invoke(app, ["llm", "ping"])
        assert result.exit_code == EXIT_RUN_FAILED
        assert "LLMConfigError: ANTHROPIC_API_KEY is not set" in result.stderr

    def test_config_show_hides_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-very-secret")
        result = runner.invoke(app, ["config", "show"])
        data = json.loads(result.stdout)
        assert data["anthropic_api_key"] == "set"
        assert data["openai_api_key"] == "not set"
        assert data["llm"]["provider"] == "fake"
        assert "sk-ant-very-secret" not in result.stdout


def test_python_dash_m_entry_point() -> None:
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-m", "agentforge", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == f"agentforge {__version__}"
