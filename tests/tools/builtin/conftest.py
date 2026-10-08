"""Fixtures: a small fake project to point the workspace tools at."""

from pathlib import Path

import pytest

from agentforge.tools import ToolContext, ToolPermission
from agentforge.tools.builtin import Workspace

ALL_PERMISSIONS = frozenset(ToolPermission)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "app" / "main.py").write_text(
        "import os\n\n\ndef handler(event):\n    return os.environ['TOKEN']\n", encoding="utf-8"
    )
    (root / "src" / "app" / "util.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (root / "README.md").write_text("# Demo\nA demo project.\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=super-secret\n", encoding="utf-8")
    (root / "certs").mkdir()
    (root / "certs" / "server.pem").write_text("-----BEGIN-----\n", encoding="utf-8")
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "site.py").write_text("ignored = True\n", encoding="utf-8")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    (tmp_path / "outside.txt").write_text("outside the workspace\n", encoding="utf-8")
    return root


@pytest.fixture
def workspace(project: Path) -> Workspace:
    return Workspace(project)


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(allowed=ALL_PERMISSIONS)
