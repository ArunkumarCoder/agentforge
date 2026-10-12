"""The shared pytest runner: isolation, timeouts, coverage, extra pytest options."""

import os
from pathlib import Path

import pytest

from agentforge.tools.pytest_runner import run_pytest, shadow_files

CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"


async def test_reports_counts_and_coverage() -> None:
    test = "from calc import add\n\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"
    result = await run_pytest(
        files={"calc.py": CALC, "tests/test_calc.py": test},
        tests=["tests/test_calc.py"],
        measure="calc.py",
    )
    assert result.passed
    assert (result.tests, result.failures, result.skipped) == (1, 0, 0)
    assert result.coverage == 75.0  # 3 of 4 statements: sub's body never runs


async def test_extra_args_run_doctests() -> None:
    module = (
        'def double(x):\n    """Twice x.\n\n    >>> double(2)\n    5\n    """\n    return 2 * x\n'
    )
    result = await run_pytest(
        files={"maths.py": module}, tests=["maths.py"], measure=None, args=["--doctest-modules"]
    )
    assert not result.passed
    assert result.tests == 1
    assert "Expected:" in result.output


async def test_runner_hides_secrets_and_enforces_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTFORGE_LLM__API_KEY", "not-a-real-key")
    leak = (
        "import os\n\n\ndef test_env() -> None:\n"
        "    assert 'AGENTFORGE_LLM__API_KEY' not in os.environ\n"
    )
    assert (
        await run_pytest(
            files={"tests/test_env.py": leak}, tests=["tests/test_env.py"], measure=None
        )
    ).passed
    assert "AGENTFORGE_LLM__API_KEY" in os.environ  # still set here, just not passed on

    slow = "import time\n\n\ndef test_slow() -> None:\n    time.sleep(30)\n"
    result = await run_pytest(
        files={"tests/test_slow.py": slow}, tests=["tests/test_slow.py"], measure=None, timeout_s=3
    )
    assert not result.passed
    assert "timeout" in result.output


async def test_runner_refuses_paths_outside_the_project() -> None:
    with pytest.raises(ValueError, match="outside the project"):
        await run_pytest(files={"../x.py": ""}, tests=[], measure=None)


def test_shadow_files_adds_the_packages_above_the_target(tmp_path: Path) -> None:
    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("A = 1\n")
    (tmp_path / "pkg" / "sub" / "__init__.py").write_text("B = 2\n")
    files = shadow_files(tmp_path, "pkg/sub/mod.py", "NEW = 1\n")
    assert files == {
        "pkg/sub/mod.py": "NEW = 1\n",
        "pkg/sub/__init__.py": "B = 2\n",
        "pkg/__init__.py": "A = 1\n",
    }


def test_shadow_files_skips_missing_inits_and_top_level_targets(tmp_path: Path) -> None:
    assert shadow_files(tmp_path, "mod.py", "x = 1\n") == {"mod.py": "x = 1\n"}
    assert shadow_files(tmp_path, "nopkg/mod.py", "x = 1\n") == {"nopkg/mod.py": "x = 1\n"}
