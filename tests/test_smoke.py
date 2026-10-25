"""Day-1 smoke test: proves the package imports and the test runner is wired up."""

import agentforge


def test_package_imports() -> None:
    assert agentforge.__version__ == "0.1.0"


def test_the_code_and_pyproject_declare_the_same_version() -> None:
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"]
    assert agentforge.__version__ == declared
