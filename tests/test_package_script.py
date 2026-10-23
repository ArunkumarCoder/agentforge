"""The pure parts of ``scripts/check_package.py``.

The full build-install-run check takes a minute, so it runs in CI (``package`` job) and with
``make package``, not here. These tests cover the decisions it makes about file lists.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_package.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_package", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_package"] = module
    spec.loader.exec_module(module)
    return module


GOOD_WHEEL = [
    "agentforge/__init__.py",
    "agentforge/agents/echo/prompt.md",
    "agentforge-0.1.0.dist-info/entry_points.txt",
]


def test_data_files_lists_prompts_and_cases_but_not_python(script: ModuleType) -> None:
    files = script.data_files()
    assert any(f.endswith("prompt.md") for f in files)
    assert any(f.endswith(".json") for f in files)
    assert not any(f.endswith((".py", ".pyc")) for f in files)
    assert all(f.startswith("agentforge/") for f in files)


def test_a_good_wheel_has_no_problems(script: ModuleType) -> None:
    assert script.check_wheel(GOOD_WHEEL, ["agentforge/agents/echo/prompt.md"]) == []


def test_a_missing_data_file_is_reported(script: ModuleType) -> None:
    problems = script.check_wheel(GOOD_WHEEL, ["agentforge/agents/x/prompt.md"])
    assert problems == ["missing from the wheel: agentforge/agents/x/prompt.md"]


def test_a_wheel_without_entry_points_or_init_is_reported(script: ModuleType) -> None:
    problems = script.check_wheel(["agentforge/other.py"], [])
    assert len(problems) == 2
    assert any("entry points" in p for p in problems)
    assert any("__init__" in p for p in problems)


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        "agentforge/.env",
        ".env.local",
        "docs/prompts/day-01.md",
        "x/docs/prompts/day-01.md",
        "agentforge/__pycache__/a.pyc",
        ".coverage",
        "junit.xml",
        "htmlcov/index.html",
    ],
)
def test_forbidden_files_are_caught(script: ModuleType, name: str) -> None:
    assert script.check_wheel([*GOOD_WHEEL, name], []) == [f"wheel contains {name}"]


def test_env_example_is_allowed(script: ModuleType) -> None:
    assert script.check_wheel([*GOOD_WHEEL, ".env.example"], []) == []


def test_sdist_prefix_is_ignored_and_pyproject_is_required(script: ModuleType) -> None:
    good = ["agentforge-0.1.0/pyproject.toml", "agentforge-0.1.0/src/agentforge/__init__.py"]
    assert script.check_sdist(good) == []
    assert script.check_sdist(["agentforge-0.1.0/docs/prompts/day-01.md", *good]) == [
        "source distribution contains docs/prompts/day-01.md"
    ]
    assert script.check_sdist(["agentforge-0.1.0/README.md"]) == [
        "the source distribution has no pyproject.toml"
    ]


def test_clean_environment_drops_settings_and_keys(
    script: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTFORGE_ENV", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setenv("KEEP_ME", "1")
    env = script.clean_environment()
    assert env["KEEP_ME"] == "1"
    assert not [k for k in env if k.startswith("AGENTFORGE_")]
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
