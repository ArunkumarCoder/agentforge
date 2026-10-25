"""`agentforge doctor`: every check, in every state it can report."""

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentforge import doctor
from agentforge.cli.main import EXIT_RUN_FAILED, app
from agentforge.config import (
    ApiSettings,
    LLMSettings,
    SandboxSettings,
    Settings,
    StorageSettings,
    get_settings,
)
from agentforge.doctor import Finding, Status, diagnose, exit_code
from agentforge.evals import all_suites
from agentforge.evals.baseline import Baseline, CaseBaseline
from agentforge.evals.runner import CaseStatus

runner = CliRunner()


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def find(findings: list[Finding], area: str, name: str) -> Finding:
    (match,) = [f for f in findings if (f.area, f.name) == (area, name)]
    return match


# --------------------------------------------------------------------------- runtime


def test_python_version(monkeypatch: pytest.MonkeyPatch) -> None:
    (ok,) = doctor.check_python()
    assert ok.status is Status.OK
    assert "agentforge" in ok.detail
    monkeypatch.setattr(doctor, "PYTHON", (3, 11, 9))
    (old,) = doctor.check_python()
    assert old.status is Status.FAIL
    assert "needs 3.12 or newer" in old.detail


def test_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    assert doctor.check_dependencies()[0].status is Status.OK
    monkeypatch.setitem(doctor.REQUIRED_MODULES, "no_such_module_xyz", "a test")
    (missing,) = doctor.check_dependencies()
    assert missing.status is Status.FAIL
    assert "no_such_module_xyz (a test)" in missing.detail
    assert "uv sync" in missing.hint


# --------------------------------------------------------------------------- model


def test_the_fake_provider_is_a_warning() -> None:
    (finding,) = doctor.check_llm(settings())
    assert finding.status is Status.WARN
    assert "fake provider" in finding.detail
    assert "API key" in finding.hint


def test_a_real_provider_without_its_key_fails_with_the_reason() -> None:
    llm = LLMSettings(provider="anthropic", model="claude-haiku-4-5")  # type: ignore[arg-type]
    (finding,) = doctor.check_llm(settings(llm=llm))
    assert finding.status is Status.FAIL
    assert "ANTHROPIC_API_KEY is not set" in finding.detail


def test_a_configured_provider_is_ok_and_a_priced_model_has_no_warning() -> None:
    llm = LLMSettings(provider="anthropic", model="claude-haiku-4-5-20251001")  # type: ignore[arg-type]
    findings = doctor.check_llm(settings(llm=llm, anthropic_api_key="x"))  # type: ignore[arg-type]
    assert [f.status for f in findings] == [Status.OK]
    assert "anthropic/claude-haiku-4-5-20251001" in findings[0].detail


def test_an_unpriced_model_warns_that_costs_will_read_zero() -> None:
    llm = LLMSettings(provider="openai", model="gpt-unknown-9")  # type: ignore[arg-type]
    findings = doctor.check_llm(settings(llm=llm, openai_api_key="x"))  # type: ignore[arg-type]
    price = find(findings, "llm", "price")
    assert price.status is Status.WARN
    assert "$0.00" in price.detail


def test_a_local_model_needs_no_key_and_no_price() -> None:
    llm = LLMSettings(provider="ollama", model="llama3.2")  # type: ignore[arg-type]
    findings = doctor.check_llm(settings(llm=llm))
    assert [f.status for f in findings] == [Status.OK]


async def test_ping_skips_the_fake_provider_and_reports_real_errors() -> None:
    findings = await diagnose(settings(), ping=True)
    assert find(findings, "llm", "ping").status is Status.WARN
    llm = LLMSettings(provider="openai", model="gpt-5-mini")  # type: ignore[arg-type]
    failed = await doctor.ping_llm(settings(llm=llm))  # no key: fails before any network call
    assert failed.status is Status.FAIL
    assert "LLMConfigError" in failed.detail


async def test_ping_reports_latency_and_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentforge.llm import FakeProvider

    monkeypatch.setattr(doctor, "create_provider", lambda _s: FakeProvider(script=["pong"]))
    finding = await doctor.ping_llm(settings())
    assert finding.status is Status.OK
    assert "answered in" in finding.detail
    assert " in / " in finding.detail


# --------------------------------------------------------------------------- storage, sandbox, api


def test_storage_off_and_on(tmp_path: Path) -> None:
    (off,) = doctor.check_storage(settings())
    assert (off.status, "off" in off.detail) == (Status.OK, True)
    url = f"sqlite:///{tmp_path / 'runs.db'}"
    (on,) = doctor.check_storage(settings(storage=StorageSettings(url=url)))  # type: ignore[arg-type]
    assert on.status is Status.OK
    assert "sqlite: 0 run(s) saved, inputs are stored" in on.detail
    private = StorageSettings(url=url, store_inputs=False)  # type: ignore[arg-type]
    assert "inputs are not stored" in doctor.check_storage(settings(storage=private))[0].detail


def test_an_unreachable_database_fails_without_the_password() -> None:
    bad = StorageSettings(url="postgresql://u:hunter2@127.0.0.1:1/none")  # type: ignore[arg-type]
    (finding,) = doctor.check_storage(settings(storage=bad))
    assert finding.status is Status.FAIL
    assert "cannot open the run store" in finding.detail
    assert "hunter2" not in finding.detail + finding.hint


def test_sandbox_states(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "IS_WINDOWS", False)
    (on,) = doctor.check_sandbox(settings())
    assert on.status is Status.OK
    assert "120s CPU, 2048 MB memory" in on.detail
    off = settings(sandbox=SandboxSettings(enabled=False))
    assert doctor.check_sandbox(off)[0].status is Status.WARN
    monkeypatch.setattr(doctor, "IS_WINDOWS", True)
    (windows,) = doctor.check_sandbox(settings())
    assert windows.status is Status.WARN
    assert "Windows" in windows.detail


def test_api_exposure() -> None:
    assert doctor.check_api(settings())[0].status is Status.OK
    open_host = settings(api=ApiSettings(host="0.0.0.0"))  # noqa: S104
    assert doctor.check_api(open_host)[0].status is Status.FAIL
    keyed = settings(api=ApiSettings(host="0.0.0.0", api_key="k"))  # noqa: S104  # type: ignore[arg-type]
    assert doctor.check_api(keyed)[0].status is Status.WARN
    half = settings(api=ApiSettings(allow_repo_paths=True))
    findings = doctor.check_api(half)
    assert find(findings, "api", "repo_path").status is Status.WARN


def test_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    both = doctor.check_tools()
    assert {f.name for f in both} == {"git", "psql"}
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)
    nothing = {f.name: f for f in doctor.check_tools()}
    assert nothing["git"].status is Status.WARN
    assert nothing["psql"].status is Status.OK  # optional
    assert "skips its optional live check" in nothing["psql"].detail


# --------------------------------------------------------------------------- agents and baselines


def test_agents_and_their_suites(monkeypatch: pytest.MonkeyPatch) -> None:
    registry_check, suites_check = doctor.check_agents()
    assert registry_check.status is Status.OK
    assert "13 agents (12 use a model)" in registry_check.detail
    assert suites_check.status is Status.OK
    monkeypatch.setattr(doctor, "all_suites", lambda: {"code-review": object()})
    _, partial = doctor.check_agents()
    assert partial.status is Status.WARN
    assert "no golden cases for:" in partial.detail
    assert "bug-fix" in partial.detail


def write_baseline(folder: Path, agent: str, model: str = "m1") -> None:
    from datetime import UTC, datetime

    case = CaseBaseline(status=CaseStatus.PASSED, score=1.0)
    Baseline(
        agent=agent,
        provider="openai",
        model=model,
        recorded_at=datetime(2026, 10, 23, tzinfo=UTC),
        cases={"a": case},
    ).save(folder)


def test_baselines_none_some_all_and_broken(tmp_path: Path) -> None:
    folder = tmp_path / "baselines"
    (none,) = doctor.check_baselines(settings(), folder)
    assert none.status is Status.WARN
    assert "eval run all --save-baseline" in none.hint
    write_baseline(folder, "code-review")
    (some,) = doctor.check_baselines(settings(), folder)
    assert some.status is Status.WARN
    assert "1/12 recorded" in some.detail
    assert "bug-fix" in some.detail
    for agent in all_suites():
        write_baseline(folder, agent)
    (everything,) = doctor.check_baselines(settings(), folder)
    assert everything.status is Status.OK
    assert "all 12 recorded (openai/m1, 2026-10-23)" in everything.detail
    (folder / "bug-fix.json").write_text("{nope", encoding="utf-8")
    (broken,) = doctor.check_baselines(settings(), folder)
    assert broken.status is Status.FAIL
    assert "unreadable: bug-fix" in broken.detail


# --------------------------------------------------------------------------- the whole thing


def test_a_crashing_check_becomes_a_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[Finding]:
        raise RuntimeError("kaput")

    (finding,) = doctor._guard("tools", boom)
    assert finding.status is Status.FAIL
    assert "RuntimeError: kaput" in finding.detail


async def test_diagnose_covers_every_area(tmp_path: Path) -> None:
    findings = await diagnose(settings(), baseline_dir=tmp_path)
    assert {f.area for f in findings} == {
        "runtime",
        "llm",
        "storage",
        "sandbox",
        "analysis",
        "api",
        "tools",
        "agents",
        "evals",
    }
    assert exit_code(findings) == 0  # warnings (fake model, no baselines) do not fail it


def test_exit_code_is_one_only_for_failures() -> None:
    warn = Finding(area="a", name="b", status=Status.WARN, detail="")
    fail = Finding(area="a", name="c", status=Status.FAIL, detail="")
    assert exit_code([warn]) == 0
    assert exit_code([warn, fail]) == 1
    assert exit_code([]) == 0


# --------------------------------------------------------------------------- the command


@pytest.fixture(autouse=True)
def fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()


def test_the_command_prints_a_table_and_hints() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "agentforge doctor" in result.stdout
    assert "fake provider" in result.stdout
    assert "evals/baselines:" in result.stdout  # the hint line
    assert "0 failure(s)" in result.stdout


def test_the_command_exits_1_on_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_API__HOST", "0.0.0.0")  # noqa: S104
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == EXIT_RUN_FAILED
    assert "FAIL" in result.stdout
    assert "1 failure(s)" in result.stdout


def test_json_output_and_a_custom_baseline_folder(tmp_path: Path) -> None:
    folder = tmp_path / "mine"
    for agent in all_suites():
        write_baseline(folder, agent)
    result = runner.invoke(app, ["doctor", "--json", "--baseline-dir", str(folder)])
    assert result.exit_code == 0
    findings = json.loads(result.stdout)
    baselines = next(f for f in findings if f["name"] == "baselines")
    assert baselines["status"] == "ok"
    assert {f["status"] for f in findings} <= {"ok", "warn", "fail"}


def test_ping_with_the_fake_provider_is_skipped_not_called() -> None:
    result = runner.invoke(app, ["doctor", "--ping"])
    assert result.exit_code == 0
    assert "skipped: the fake provider" in " ".join(result.stdout.split())


def test_analysis_needs_pandas_and_reports_how_the_network_is_cut_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor, "network_namespace_available", lambda: True)
    (ok,) = doctor.check_analysis()
    assert ok.status is Status.OK
    assert "no network at all" in ok.detail
    monkeypatch.setattr(doctor, "network_namespace_available", lambda: False)
    (guard,) = doctor.check_analysis()
    assert guard.status is Status.WARN
    assert "socket module" in guard.detail
    real_find_spec = doctor.importlib.util.find_spec
    monkeypatch.setattr(
        doctor.importlib.util,
        "find_spec",
        lambda name: None if name == "pandas" else real_find_spec(name),
    )
    (missing,) = doctor.check_analysis()
    assert missing.status is Status.WARN
    assert "pandas" in missing.detail
    assert "--extra analysis" in missing.hint
