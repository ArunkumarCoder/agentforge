"""Day-1 smoke test: proves the package imports and the test runner is wired up."""

import agentforge


def test_package_imports() -> None:
    assert agentforge.__version__ == "0.0.1"
