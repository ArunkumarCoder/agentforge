"""Path safety: everything outside the workspace or secret must be refused."""

import os
from pathlib import Path

import pytest

from agentforge.tools.builtin import Workspace, WorkspaceError


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "src/../../outside.txt",
        "src/app/../../../outside.txt",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "\\\\server\\share\\x",
    ],
)
def test_escapes_are_blocked(workspace: Workspace, path: str) -> None:
    with pytest.raises(WorkspaceError):
        workspace.resolve(path)


@pytest.mark.parametrize(
    "path", [".env", ".env.production", "certs/server.pem", ".git/config", "keys/id_rsa"]
)
def test_secrets_and_git_internals_are_blocked(workspace: Workspace, path: str) -> None:
    with pytest.raises(WorkspaceError, match="not allowed"):
        workspace.resolve(path)


def test_normal_paths_resolve_inside_root(workspace: Workspace, project: Path) -> None:
    assert workspace.resolve("src/app/main.py") == (project / "src/app/main.py").resolve()
    assert workspace.resolve("./src/../README.md") == (project / "README.md").resolve()
    assert workspace.resolve("") == project.resolve()
    assert workspace.relative(workspace.resolve("src/app")) == "src/app"
    assert workspace.relative(workspace.root) == "."


def test_symlink_escaping_the_root_is_blocked(workspace: Workspace, project: Path) -> None:
    link = project / "sneaky"
    try:
        os.symlink(project.parent / "outside.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this system")
    with pytest.raises(WorkspaceError, match="outside the workspace"):
        workspace.resolve("sneaky")


def test_root_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        Workspace(tmp_path / "missing")
