"""``agentforge doctor``: is this installation ready to do real work?

Each check answers one question and returns a :class:`Finding` that is ``ok``, a ``warn``
(works, but you probably want to fix it) or a ``fail`` (something will not work). Nothing
here changes anything, and nothing touches the network unless ``ping`` is asked for, which
sends one short request to the configured model.

The point is to catch, in ten seconds and in plain words, the problems that otherwise show up
half way through a paid eval run: a missing API key, a model with no known price (cost would
read $0), a database that is unreachable, no baseline to compare against.
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from agentforge import __version__
from agentforge.config import LLMProvider as ProviderName
from agentforge.config import Settings
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import registry
from agentforge.core.schemas import Contract, Message
from agentforge.evals import BaselineError, all_suites, load_baseline
from agentforge.llm import LLMError, LLMRequest, PriceTable, create_provider
from agentforge.storage import StorageError, open_store
from agentforge.tools.pyexec import network_namespace_available

MIN_PYTHON = (3, 12)
IS_WINDOWS = sys.platform == "win32"
PYTHON = sys.version_info
REQUIRED_MODULES = {
    "bandit": "the Security Audit agent",
    "pglast": "the Database Schema agent",
    "openapi_spec_validator": "the API Design agent",
    "sqlalchemy": "run history and the Database Schema agent",
    "psycopg": "PostgreSQL run history",
    "fastapi": "the HTTP API",
    "uvicorn": "the HTTP API",
}
LOCAL_MODELS = {ProviderName.OLLAMA, ProviderName.FAKE}


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


class Finding(Contract):
    area: str
    name: str
    status: Status
    detail: str
    hint: str = ""


def _ok(area: str, name: str, detail: str) -> Finding:
    return Finding(area=area, name=name, status=Status.OK, detail=detail)


def _warn(area: str, name: str, detail: str, hint: str = "") -> Finding:
    return Finding(area=area, name=name, status=Status.WARN, detail=detail, hint=hint)


def _fail(area: str, name: str, detail: str, hint: str = "") -> Finding:
    return Finding(area=area, name=name, status=Status.FAIL, detail=detail, hint=hint)


# --------------------------------------------------------------------------- checks


def check_python() -> list[Finding]:
    version = f"{PYTHON[0]}.{PYTHON[1]}.{PYTHON[2]}"
    if tuple(PYTHON[:2]) < MIN_PYTHON:
        need = ".".join(map(str, MIN_PYTHON))
        return [_fail("runtime", "python", f"{version}, needs {need} or newer")]
    return [_ok("runtime", "python", f"{version} on {sys.platform}, agentforge {__version__}")]


def check_dependencies() -> list[Finding]:
    missing = [
        f"{module} ({use})"
        for module, use in REQUIRED_MODULES.items()
        if importlib.util.find_spec(module) is None
    ]
    if missing:
        return [
            _fail(
                "runtime",
                "dependencies",
                "missing: " + ", ".join(missing),
                "run `uv sync` to install the locked dependencies",
            )
        ]
    return [_ok("runtime", "dependencies", f"all {len(REQUIRED_MODULES)} required packages found")]


def check_llm(settings: Settings) -> list[Finding]:
    llm = settings.llm
    label = f"{llm.provider.value}/{llm.model}"
    if llm.provider is ProviderName.FAKE:
        return [
            _warn(
                "llm",
                "provider",
                "the fake provider is configured: fine for tests and demos, it cannot do real work",
                "set AGENTFORGE_LLM__PROVIDER, AGENTFORGE_LLM__MODEL and an API key in .env",
            )
        ]
    try:
        create_provider(settings)
    except LLMError as exc:
        return [
            _fail("llm", "provider", f"{label}: {exc}", "put the key in .env or the environment")
        ]
    findings = [_ok("llm", "provider", f"{label} is configured")]
    if llm.provider not in LOCAL_MODELS and PriceTable.default().price_for(llm.model) is None:
        findings.append(
            _warn(
                "llm",
                "price",
                f"no price known for '{llm.model}', so every cost will read $0.00",
                "add it to DEFAULT_PRICES in agentforge/llm/pricing.py before recording baselines",
            )
        )
    return findings


async def ping_llm(settings: Settings) -> Finding:
    """One short, real request to the configured model."""
    try:
        provider = create_provider(settings)
        response = await provider.complete(
            LLMRequest(messages=[Message.user("Reply with the single word: pong")], max_tokens=256)
        )
    except LLMError as exc:
        return _fail("llm", "ping", f"{type(exc).__name__}: {exc}")
    usage = response.usage
    return _ok(
        "llm",
        "ping",
        f"{response.model} answered in {response.latency_ms:.0f} ms "
        f"({usage.input_tokens} in / {usage.output_tokens} out, ${usage.cost_usd:.6f})",
    )


def check_storage(settings: Settings) -> list[Finding]:
    if settings.storage.url is None:
        return [
            _ok(
                "storage",
                "run history",
                "off (optional). Set AGENTFORGE_STORAGE__URL to keep every run",
            )
        ]
    try:
        store = open_store(settings)
        assert store is not None  # noqa: S101 - the URL was checked above
        try:
            total = store.list_runs(limit=1).total
            backend = store.backend
        finally:
            store.close()
    except StorageError as exc:
        return [_fail("storage", "run history", str(exc), "check AGENTFORGE_STORAGE__URL")]
    kept = "inputs are stored" if settings.storage.store_inputs else "inputs are not stored"
    return [_ok("storage", "run history", f"{backend}: {total} run(s) saved, {kept}")]


def check_sandbox(settings: Settings) -> list[Finding]:
    box = settings.sandbox
    if not box.enabled:
        return [
            _warn(
                "sandbox",
                "limits",
                "CPU, memory and file limits are switched off",
                "unset AGENTFORGE_SANDBOX__ENABLED unless you run inside a container",
            )
        ]
    if IS_WINDOWS:
        return [
            _warn(
                "sandbox",
                "limits",
                "not applied on Windows (only the timeout and output cap are)",
                "run AgentForge in WSL, a container or a VM for untrusted code",
            )
        ]
    return [
        _ok(
            "sandbox",
            "limits",
            f"{box.cpu_s}s CPU, {box.memory_mb} MB memory, {box.file_mb} MB files, "
            f"{box.output_mb} MB output",
        )
    ]


def check_analysis() -> list[Finding]:
    """What the Data Analysis agent needs: pandas, matplotlib, and how the network is cut off."""
    missing = [m for m in ("pandas", "matplotlib") if importlib.util.find_spec(m) is None]
    if missing:
        return [
            _warn(
                "analysis",
                "packages",
                "missing: " + ", ".join(missing) + " (the data-analysis agent cannot run)",
                "uv sync --extra analysis   (or pip install 'agentforge[analysis]')",
            )
        ]
    if not network_namespace_available():
        return [
            _warn(
                "analysis",
                "network",
                "pandas found; code is kept off the network by a blocked socket module only",
                "on Linux, allow unprivileged user namespaces for a real network cut-off",
            )
        ]
    return [_ok("analysis", "network", "pandas found; analysis code runs with no network at all")]


def check_api(settings: Settings) -> list[Finding]:
    api = settings.api
    findings: list[Finding] = []
    if not api.is_loopback and api.api_key is None:
        findings.append(
            _fail(
                "api",
                "exposure",
                f"{api.host} is not loopback and there is no API key: `serve` will refuse to start",
                "set AGENTFORGE_API__API_KEY, or listen on 127.0.0.1",
            )
        )
    elif not api.is_loopback:
        findings.append(
            _warn(
                "api",
                "exposure",
                f"listening on {api.host} with an API key",
                "agents run the code they are sent: keep this network private",
            )
        )
    else:
        findings.append(_ok("api", "exposure", f"loopback only ({api.host}:{api.port})"))
    if api.allow_repo_paths and api.repo_root is None:
        findings.append(
            _warn(
                "api",
                "repo_path",
                "ALLOW_REPO_PATHS is on but REPO_ROOT is not set, so repo_path is still refused",
                "set AGENTFORGE_API__REPO_ROOT",
            )
        )
    return findings


def check_tools() -> list[Finding]:
    findings = []
    if shutil.which("git") is None:
        findings.append(
            _warn("tools", "git", "not found; the Code Review agent needs it for repo_path")
        )
    else:
        findings.append(_ok("tools", "git", "found"))
    if shutil.which("psql") is None:
        findings.append(
            _ok("tools", "psql", "not installed: Database Schema skips its optional live check")
        )
    else:
        findings.append(_ok("tools", "psql", "found (optional live DDL check)"))
    return findings


def check_agents() -> list[Finding]:
    registry.discover()
    names = sorted(card.name for card in registry.cards())
    llm_agents = [n for n in names if issubclass(registry.get(n), LLMAgent)]
    suites = set(all_suites())
    uncovered = [n for n in llm_agents if n not in suites]
    findings = [_ok("agents", "registry", f"{len(names)} agents ({len(llm_agents)} use a model)")]
    if uncovered:
        findings.append(
            _warn("agents", "eval cases", "no golden cases for: " + ", ".join(uncovered))
        )
    else:
        findings.append(
            _ok("agents", "eval cases", f"every model-backed agent has a suite ({len(suites)})")
        )
    return findings


def check_baselines(settings: Settings, folder: Path) -> list[Finding]:
    suites = sorted(all_suites())
    found: dict[str, str] = {}
    broken: list[str] = []
    for name in suites:
        path = folder / f"{name}.json"
        if not path.is_file():
            continue
        try:
            found[name] = load_baseline(path).label
        except BaselineError:
            broken.append(name)
    hint = "uv run agentforge eval run all --save-baseline (needs a real model)"
    if broken:
        return [
            _fail("evals", "baselines", "unreadable: " + ", ".join(broken), f"re-record: {hint}")
        ]
    if not found:
        return [_warn("evals", "baselines", f"none recorded in {folder}", hint)]
    missing = [n for n in suites if n not in found]
    if missing:
        return [
            _warn(
                "evals",
                "baselines",
                f"{len(found)}/{len(suites)} recorded; missing: {', '.join(missing)}",
                hint,
            )
        ]
    models = sorted(set(found.values()))
    return [_ok("evals", "baselines", f"all {len(suites)} recorded ({'; '.join(models)})")]


# --------------------------------------------------------------------------- run them all


def _guard(area: str, check: Callable[[], list[Finding]]) -> list[Finding]:
    """A check that crashes is a finding, not a crash of the doctor."""
    try:
        return check()
    except Exception as exc:
        return [_fail(area, "check", f"the check itself failed: {type(exc).__name__}: {exc}")]


async def diagnose(
    settings: Settings, *, ping: bool = False, baseline_dir: Path = Path("docs/evals/baselines")
) -> list[Finding]:
    findings: list[Finding] = []
    for area, check in (
        ("runtime", check_python),
        ("runtime", check_dependencies),
        ("llm", lambda: check_llm(settings)),
        ("storage", lambda: check_storage(settings)),
        ("sandbox", lambda: check_sandbox(settings)),
        ("analysis", check_analysis),
        ("api", lambda: check_api(settings)),
        ("tools", check_tools),
        ("agents", check_agents),
        ("evals", lambda: check_baselines(settings, baseline_dir)),
    ):
        findings += _guard(area, check)
    if ping:
        if settings.llm.provider is ProviderName.FAKE:
            findings.append(_warn("llm", "ping", "skipped: the fake provider is configured"))
        else:
            findings.append(await ping_llm(settings))
    return findings


def exit_code(findings: list[Finding]) -> int:
    """1 if anything failed (warnings do not fail the command)."""
    return 1 if any(f.status is Status.FAIL for f in findings) else 0
