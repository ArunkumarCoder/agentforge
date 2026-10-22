"""`agentforge agents example` and `agents run --example`: ready-made inputs for every agent."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentforge.cli.main import EXIT_RUN_FAILED, EXIT_USAGE, _example_cases, app
from agentforge.config import get_settings
from agentforge.core.registry import registry
from agentforge.evals import all_suites

runner = CliRunner()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


def agents_with_examples() -> list[str]:
    return sorted([*all_suites(), "echo"])


@pytest.mark.parametrize("name", agents_with_examples())
def test_every_example_is_a_valid_input_for_its_agent(name: str) -> None:
    registry.discover()
    model = registry.get(name).input_model
    cases = _example_cases(name)
    assert cases, name
    for case, payload in cases.items():
        model.model_validate(payload)  # raises if the example is not accepted
        assert json.loads(json.dumps(payload)) == payload, f"{name}/{case} is not plain JSON"


def test_every_registered_agent_has_an_example() -> None:
    registry.discover()
    assert sorted(c.name for c in registry.cards()) == agents_with_examples()


def test_example_prints_json_for_the_first_case() -> None:
    result = runner.invoke(app, ["agents", "example", "echo"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"text": "hello world", "upper": False}


def test_example_for_a_suite_agent_and_a_named_case() -> None:
    first = json.loads(runner.invoke(app, ["agents", "example", "security-audit"]).stdout)
    named = json.loads(
        runner.invoke(app, ["agents", "example", "security-audit", "-c", "web_app"]).stdout
    )
    assert "files" in first and "files" in named
    assert first == json.loads(
        runner.invoke(app, ["agents", "example", "security-audit", "-c", "api_client"]).stdout
    )  # cases are sorted by name, so the first is api_client
    assert named != first


def test_list_shows_the_case_names() -> None:
    result = runner.invoke(app, ["agents", "example", "security-audit", "--list"])
    assert result.stdout.split() == [
        "api_client",
        "auth_service",
        "cli_tool",
        "data_loader",
        "web_app",
    ]
    assert runner.invoke(app, ["agents", "example", "echo", "--list"]).stdout.split() == ["hello"]


def test_unknown_agent_and_unknown_case_are_explained() -> None:
    missing = runner.invoke(app, ["agents", "example", "nope"])
    assert missing.exit_code == EXIT_USAGE
    assert "no agent named 'nope'" in " ".join(missing.stderr.split())
    bad_case = runner.invoke(app, ["agents", "example", "echo", "-c", "zzz"])
    assert bad_case.exit_code == EXIT_USAGE
    assert "no example case 'zzz' for echo (available: hello)" in " ".join(bad_case.stderr.split())


def test_an_agent_without_examples_says_which_have_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentforge.cli.main.all_suites", lambda: {})
    result = runner.invoke(app, ["agents", "example", "migration"])
    assert result.exit_code == EXIT_USAGE
    assert "no example for 'migration'. Agents with examples: echo" in " ".join(
        result.stderr.split()
    )


def test_run_with_example_runs_the_ready_made_input() -> None:
    result = runner.invoke(app, ["agents", "run", "echo", "--example", "--json"])
    assert result.exit_code == 0
    body = json.loads(result.stdout)
    assert body["status"] == "succeeded"
    assert body["output"] == {"text": "hello world", "length": 11}


def test_run_example_reaches_the_agent_with_a_real_suite_input() -> None:
    result = runner.invoke(app, ["agents", "run", "migration", "--example", "--json"])
    body = json.loads(result.stdout)
    # the fake provider cannot answer, but the input was accepted: the failure is the output's
    assert result.exit_code == EXIT_RUN_FAILED
    assert body["error"]["kind"] == "output_validation"


def test_example_piped_into_run_through_a_file(tmp_path: Path) -> None:
    payload = runner.invoke(app, ["agents", "example", "echo"]).stdout
    path = tmp_path / "in.json"
    path.write_text(payload, encoding="utf-8")
    result = runner.invoke(app, ["agents", "run", "echo", "-i", str(path), "--json"])
    assert json.loads(result.stdout)["output"]["length"] == 11
    piped = runner.invoke(app, ["agents", "run", "echo", "-i", "-", "--json"], input=payload)
    assert json.loads(piped.stdout)["output"]["length"] == 11


def test_example_cannot_be_mixed_with_other_input() -> None:
    both = runner.invoke(app, ["agents", "run", "echo", "--example", "-d", "{}"])
    assert both.exit_code == EXIT_USAGE
    assert "use --example on its own" in " ".join(both.stderr.split())
    lonely = runner.invoke(app, ["agents", "run", "echo", "-c", "hello"])
    assert lonely.exit_code == EXIT_USAGE
    assert "--case only works together with --example" in " ".join(lonely.stderr.split())
    unknown = runner.invoke(app, ["agents", "run", "echo", "--example", "-c", "zzz"])
    assert unknown.exit_code == EXIT_USAGE
