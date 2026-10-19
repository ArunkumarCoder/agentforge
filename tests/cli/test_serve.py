"""`agentforge serve` and the API settings, without starting a real server."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentforge.cli.main import EXIT_USAGE, app
from agentforge.config import ApiSettings, get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    calls: dict[str, Any] = {}

    def fake_run(application: object, **kwargs: Any) -> None:
        calls.update(kwargs, app=application)

    monkeypatch.setattr("uvicorn.run", fake_run)
    return calls


def test_serve_defaults_to_loopback(started: dict[str, Any]) -> None:
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0
    assert (started["host"], started["port"]) == ("127.0.0.1", 8000)
    assert started["app"].title == "AgentForge"
    assert "http://127.0.0.1:8000" in result.stderr


def test_serve_options_and_environment(
    started: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTFORGE_API__PORT", "9100")
    assert runner.invoke(app, ["serve"]).exit_code == 0
    assert started["port"] == 9100
    assert runner.invoke(app, ["serve", "--port", "9200"]).exit_code == 0
    assert started["port"] == 9200


def test_serve_refuses_a_public_host_without_a_key(started: dict[str, Any]) -> None:
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == EXIT_USAGE
    assert "without an API key" in " ".join(result.stderr.split())
    assert started == {}


def test_serve_allows_a_public_host_with_a_key(
    started: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTFORGE_API__API_KEY", "a-long-random-key")
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 0
    assert started["host"] == "0.0.0.0"  # noqa: S104


def test_config_show_hides_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_API__API_KEY", "a-long-random-key")
    result = runner.invoke(app, ["config", "show"])
    data = json.loads(result.stdout)
    assert data["api"]["api_key"] == "set"
    assert data["api"]["host"] == "127.0.0.1"
    assert "a-long-random-key" not in result.stdout
    monkeypatch.delenv("AGENTFORGE_API__API_KEY")
    get_settings.cache_clear()
    assert json.loads(runner.invoke(app, ["config", "show"]).stdout)["api"]["api_key"] == "not set"


def test_api_settings_rules() -> None:
    assert ApiSettings(api_key="  ").api_key is None  # blank, as in .env.example
    assert ApiSettings().is_loopback
    assert ApiSettings(host="localhost").is_loopback
    assert not ApiSettings(host="0.0.0.0").is_loopback  # noqa: S104
    with pytest.raises(ValueError, match="less than or equal to 65535"):
        ApiSettings(port=70000)
