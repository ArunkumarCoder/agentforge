"""Workspace: the one folder file tools may touch.

Every path a model asks for is resolved against the workspace root and
rejected when it:

- is absolute (``/etc/passwd``, ``C:\\Windows``),
- escapes the root with ``..`` or through a symlink,
- points at secrets or internals (``.env*``, ``.git/``, keys and certificates).

The checks run on the *resolved* path, so ``a/../../x`` and symlinks that lead
outside the root are caught as well as the obvious cases.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath, PureWindowsPath

from agentforge.tools.base import ToolError

# Never readable through tools, even inside the workspace.
DENIED_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".pypirc",
)
DENIED_DIRS = frozenset({".git"})
# Skipped when listing or searching (noise, not secrets).
IGNORED_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".cache",
        "htmlcov",
        "dist",
        "build",
    }
)


class WorkspaceError(ToolError):
    """A path is outside the workspace or not allowed. The message is shown to the model."""


class Workspace:
    """A root folder plus the rules for which paths inside it tools may use."""

    def __init__(self, root: Path | str) -> None:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise ValueError(f"workspace root is not a directory: {root}")
        self.root = resolved

    def resolve(self, relative: str) -> Path:
        """Turn a model-supplied relative path into a safe absolute path."""
        text = relative.strip() or "."
        if PurePosixPath(text).is_absolute() or PureWindowsPath(text).is_absolute():
            raise WorkspaceError(f"Use a path relative to the workspace, not '{relative}'.")
        candidate = (self.root / text).resolve()
        if not candidate.is_relative_to(self.root):
            raise WorkspaceError(f"'{relative}' is outside the workspace.")
        if self.is_denied(candidate):
            raise WorkspaceError(f"Access to '{relative}' is not allowed.")
        return candidate

    def relative(self, path: Path) -> str:
        """Workspace-relative path with forward slashes, as shown to the model."""
        return path.relative_to(self.root).as_posix() or "."

    def is_denied(self, path: Path) -> bool:
        parts = path.relative_to(self.root).parts
        if any(part in DENIED_DIRS for part in parts):
            return True
        return any(fnmatch.fnmatch(path.name, pattern) for pattern in DENIED_PATTERNS)

    def is_ignored(self, path: Path) -> bool:
        return any(part in IGNORED_DIRS for part in path.relative_to(self.root).parts)
