"""Run pytest on model-written code in a scratch directory and measure coverage of one file.

Used by the Test Generation agent (generated tests) and the Documentation agent
(doctests). The code is written by a model, so they run with some care, though this is
not a sandbox (containers come later):

- a fresh temporary directory; nothing is written to the user's project,
  and ``sys.dont_write_bytecode`` keeps ``__pycache__`` out of it too,
- a separate, isolated interpreter (``python -I``): no user site-packages,
  no ``PYTHON*`` variables, no script directory on ``sys.path``,
- a minimal environment, so API keys and other secrets are not visible,
- a timeout, after which the process is killed.

pytest's own config files in the project are ignored (``-c`` points at an
empty ini), so the run behaves the same everywhere. Coverage uses the
``coverage`` package when it is installed; otherwise ``coverage`` is ``None``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import Field

from agentforge.core.schemas import Contract
from agentforge.tools.builtin.process import run_process


class PytestRun(Contract):
    """What happened when AgentForge ran the generated tests."""

    passed: bool
    tests: int = Field(ge=0)
    failures: int = Field(ge=0, description="Failed tests plus errors")
    skipped: int = Field(ge=0)
    coverage: float | None = Field(
        default=None, description="Percent of the target's statements run by the tests"
    )
    output: str = Field(default="", description="The end of pytest's output")


DEFAULT_TIMEOUT_S = 60.0
MAX_OUTPUT_CHARS = 6_000
_ENV_KEYS = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL",
)  # fmt: skip

_BOOTSTRAP = """\
import io
import json
import sys

sys.dont_write_bytecode = True
sys.path[:0] = {paths!r}
measure = {measure!r}
cov = None
if measure:
    try:
        import coverage
    except ImportError:
        coverage = None
    if coverage is not None:
        import warnings

        warnings.filterwarnings("ignore", category=coverage.exceptions.CoverageWarning)
        cov = coverage.Coverage(data_file=None, include=[measure])
        cov.start()

import pytest

code = pytest.main({args!r})
result = {{"coverage": None}}
if cov is not None:
    cov.stop()
    try:
        result["coverage"] = round(cov.report(file=io.StringIO()), 1)
    except coverage.exceptions.NoDataError:
        result["coverage"] = 0.0
with open({result_file!r}, "w", encoding="utf-8") as fh:
    json.dump(result, fh)
sys.exit(int(code))
"""


def pytest_available() -> bool:
    return importlib.util.find_spec("pytest") is not None


def coverage_available() -> bool:
    return importlib.util.find_spec("coverage") is not None


def _safe_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEYS}
    env["PYTHONIOENCODING"] = "utf-8"  # ignored by -I for config, but harmless
    return env


def _tail(text: str) -> str:
    text = text.strip()
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return "...\n" + text[-MAX_OUTPUT_CHARS:]


def _counts(report: Path) -> tuple[int, int, int] | None:
    """(tests, failures + errors, skipped) from a JUnit XML report, or None if missing."""
    try:
        root = ET.parse(report).getroot()  # noqa: S314 - our own pytest report
    except (OSError, ET.ParseError):
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = failures = skipped = 0
    for suite in suites:
        tests += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0)) + int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
    return tests, failures, skipped


async def run_pytest(
    *,
    files: Mapping[str, str],
    tests: Sequence[str],
    measure: str | Path | None,
    extra_paths: Sequence[str | Path] = (),
    args: Sequence[str] = (),
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> PytestRun:
    """Write ``files`` to a temp project, run the ``tests`` among them, report the outcome.

    ``measure`` is the file whose coverage is reported: a path relative to the
    temp project, or an absolute path (for a module in a repository that is
    importable through ``extra_paths``). ``args`` are extra pytest options,
    e.g. ``--doctest-modules`` to run the examples in a module's docstrings.
    """
    with tempfile.TemporaryDirectory(prefix="agentforge-tests-") as tmp:
        base = Path(tmp)
        project, scratch = base / "project", base / "run"
        project.mkdir()
        scratch.mkdir()
        for rel, content in files.items():
            target = (project / rel).resolve()
            if not target.is_relative_to(project.resolve()):
                raise ValueError(f"refusing to write outside the project: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        paths = [str(project), str(project / "src")]
        paths += [str(Path(p)) for p in extra_paths]
        if measure is not None:
            measure_path = Path(measure)
            if not measure_path.is_absolute():
                measure_path = project / measure_path
            measure = str(measure_path.resolve())
        ini, report, result_file = scratch / "pytest.ini", scratch / "junit.xml", scratch / "r.json"
        ini.write_text("[pytest]\n", encoding="utf-8")
        args = [
            *(str(project / t) for t in tests),
            "-q",
            "--tb=short",
            "-rfE",
            "-p",
            "no:cacheprovider",
            "-c",
            str(ini),
            "--rootdir",
            str(project),
            f"--junitxml={report}",
            *args,
        ]
        script = scratch / "bootstrap.py"
        script.write_text(
            _BOOTSTRAP.format(
                paths=paths, measure=measure, args=args, result_file=str(result_file)
            ),
            encoding="utf-8",
        )
        proc = await run_process(
            [sys.executable, "-I", str(script)],
            cwd=str(project),
            timeout_s=timeout_s,
            env=_safe_env(),
            inherit_env=False,
        )
        output = _tail(f"{proc.stdout}\n{proc.stderr}".replace(str(project) + os.sep, ""))
        if proc.timed_out:
            output = f"pytest was stopped after {timeout_s:.0f}s (timeout).\n{output}"
        counts = _counts(report)
        coverage: float | None = None
        try:
            coverage = json.loads(result_file.read_text(encoding="utf-8"))["coverage"]
        except (OSError, ValueError, KeyError):
            coverage = None

    tests_run, failures, skipped = counts or (0, 0, 0)
    return PytestRun(
        passed=proc.exit_code == 0 and tests_run > 0 and not proc.timed_out,
        tests=tests_run,
        failures=failures,
        skipped=skipped,
        coverage=coverage,
        output=output,
    )
