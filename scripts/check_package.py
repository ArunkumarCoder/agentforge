"""Build AgentForge and prove the built package works on its own.

    uv run python scripts/check_package.py [--keep]

What it does, in a throw-away folder:

1. ``uv build`` makes the wheel and the source distribution.
2. The wheel must contain every non-Python file that ships with the package (prompts, eval
   cases) and the distributions must not contain things that must never be published
   (``.env``, ``docs/prompts``, caches, test output).
3. The wheel is installed into a fresh virtual environment, and from an *empty* folder, so
   nothing is picked up from the repository, the CLI is run: version, agents, eval cases, an
   example run, config and ``doctor``.

Exit code 0 means the release artefacts are sound; 1 lists what is wrong. It is what the
"package" job in CI and ``make package`` run. Works on Linux, macOS and Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "agentforge"
FORBIDDEN = (
    ".env",
    "docs/prompts/",
    ".cache/",
    "htmlcov/",
    ".coverage",
    "junit.xml",
    "coverage.xml",
)
EXPECTED_AGENTS = 11  # ten engineering agents and echo
EXPECTED_SUITES = 10
EXPECTED_CASES_PER_SUITE = 5
SECRET_ENV_PREFIXES = ("AGENTFORGE_",)
SECRET_ENV_NAMES = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}


# --------------------------------------------------------------------------- pure checks


def data_files(src: Path = SRC) -> list[str]:
    """Every file under the package that is not Python source or a cache, as wheel paths."""
    found = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.suffix in {".py", ".pyc"} or "__pycache__" in path.parts:
            continue
        found.append("agentforge/" + path.relative_to(src).as_posix())
    return found


def check_wheel(names: Sequence[str], expected_data: Sequence[str]) -> list[str]:
    """Problems with a wheel's file list (``[]`` means fine)."""
    problems = [f"missing from the wheel: {name}" for name in expected_data if name not in names]
    problems += _forbidden(names, "wheel")
    if not any(n.endswith(".dist-info/entry_points.txt") for n in names):
        problems.append("the wheel has no entry points (the `agentforge` command)")
    if not any(n == "agentforge/__init__.py" for n in names):
        problems.append("the wheel has no agentforge/__init__.py")
    return problems


def check_sdist(names: Sequence[str]) -> list[str]:
    """Problems with a source distribution's file list."""
    stripped = [name.split("/", 1)[1] if "/" in name else name for name in names]
    problems = _forbidden(stripped, "source distribution")
    if "pyproject.toml" not in stripped:
        problems.append("the source distribution has no pyproject.toml")
    return problems


def _forbidden(names: Sequence[str], what: str) -> list[str]:
    bad = []
    for name in names:
        parts = name.split("/")
        leaf = parts[-1]
        if (
            leaf == ".env"
            or (leaf.startswith(".env.") and leaf != ".env.example")
            or any(name.startswith(prefix) or f"/{prefix}" in name for prefix in FORBIDDEN[1:4])
            or leaf in {".coverage", "junit.xml", "coverage.xml"}
            or "__pycache__" in parts
        ):
            bad.append(f"{what} contains {name}")
    return bad


def clean_environment() -> dict[str, str]:
    """This process's environment without AgentForge settings or API keys, so the check does not
    depend on (or leak) whoever runs it."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(SECRET_ENV_PREFIXES) and k not in SECRET_ENV_NAMES
    }


# --------------------------------------------------------------------------- steps


def run(
    argv: Sequence[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - our own commands
        list(argv), cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=600
    )


def build(out: Path) -> tuple[Path, Path]:
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required (https://docs.astral.sh/uv/)")
    done = run([uv, "build", "--out-dir", str(out)], cwd=ROOT)
    if done.returncode != 0:
        raise SystemExit(f"uv build failed:\n{done.stderr or done.stdout}")
    wheel = next(out.glob("*.whl"))
    sdist = next(out.glob("*.tar.gz"))
    return wheel, sdist


def sdist_names(sdist: Path) -> list[str]:
    import tarfile

    with tarfile.open(sdist) as tar:
        return tar.getnames()


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def install(wheel: Path, venv: Path) -> Path:
    uv = shutil.which("uv") or "uv"
    made = run(
        [uv, "venv", "--python", f"{sys.version_info[0]}.{sys.version_info[1]}", str(venv)],
        cwd=ROOT,
    )
    if made.returncode != 0:
        raise SystemExit(f"could not create a virtual environment:\n{made.stderr}")
    python = venv_python(venv)
    done = run([uv, "pip", "install", "--python", str(python), str(wheel)], cwd=ROOT)
    if done.returncode != 0:
        raise SystemExit(f"could not install the wheel:\n{done.stderr or done.stdout}")
    return python


def smoke(python: Path, workdir: Path) -> list[str]:
    """Run the installed CLI from an empty folder; returns the problems found."""
    env = clean_environment()
    problems: list[str] = []

    def cli(*args: str) -> subprocess.CompletedProcess[str]:
        return run([str(python), "-m", "agentforge", *args], cwd=workdir, env=env)

    def json_of(done: subprocess.CompletedProcess[str], what: str) -> object | None:
        if done.returncode != 0:
            problems.append(
                f"{what}: exit code {done.returncode}: {(done.stderr or done.stdout)[-300:]}"
            )
            return None
        try:
            return json.loads(done.stdout)
        except json.JSONDecodeError:
            problems.append(f"{what}: output is not JSON")
            return None

    version = cli("--version")
    if version.returncode != 0 or not version.stdout.startswith("agentforge "):
        problems.append(f"--version: {version.stdout or version.stderr}")

    agents = json_of(cli("agents", "list", "--json"), "agents list")
    if isinstance(agents, list) and len(agents) != EXPECTED_AGENTS:
        problems.append(f"agents list: {len(agents)} agents, expected {EXPECTED_AGENTS}")

    suites = json_of(cli("eval", "list", "--json"), "eval list")
    if isinstance(suites, dict):
        if len(suites) != EXPECTED_SUITES:
            problems.append(f"eval list: {len(suites)} suites, expected {EXPECTED_SUITES}")
        short = [n for n, cases in suites.items() if len(cases) != EXPECTED_CASES_PER_SUITE]
        if short:
            problems.append(f"eval list: wrong number of cases for {', '.join(short)}")

    run_result = json_of(
        cli("agents", "run", "echo", "--example", "--json"), "agents run echo --example"
    )
    if isinstance(run_result, dict) and run_result.get("status") != "succeeded":
        problems.append(f"agents run echo --example: status {run_result.get('status')}")

    example = json_of(cli("agents", "example", "security-audit"), "agents example security-audit")
    if example is not None and not isinstance(example, dict):
        problems.append("agents example security-audit: not a JSON object")

    config = cli("config", "show")
    if config.returncode != 0:
        problems.append(f"config show: exit code {config.returncode}")

    doctor = json_of(cli("doctor", "--json"), "doctor")
    if isinstance(doctor, list):
        failed = [
            f"{f['area']}/{f['name']}: {f['detail']}" for f in doctor if f["status"] == "fail"
        ]
        problems += [f"doctor: {item}" for item in failed]
    return problems


# --------------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build AgentForge and test the built package.")
    parser.add_argument("--keep", action="store_true", help="keep the build folder and show where")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="agentforge-package-"))
    try:
        dist, work = tmp / "dist", tmp / "empty"
        work.mkdir()
        print("building...")
        wheel, sdist = build(dist)
        print(f"  {wheel.name} ({wheel.stat().st_size // 1024} KB), {sdist.name}")
        problems = check_wheel(zipfile.ZipFile(wheel).namelist(), data_files())
        problems += check_sdist(sdist_names(sdist))
        print(f"  checked the contents: {len(problems)} problem(s)")
        if not problems:
            print("installing into a fresh environment and running the CLI from an empty folder...")
            python = install(wheel, tmp / "venv")
            problems += smoke(python, work)
            print(f"  smoke test: {len(problems)} problem(s)")
    finally:
        if args.keep:
            print(f"kept: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    if problems:
        print("\nFAILED")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("\nOK: the package builds, contains what it should, installs and runs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
