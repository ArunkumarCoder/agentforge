"""Running programs safely: a shared subprocess helper, git_diff and run_command.

No shell is ever used: commands are argument lists, so ``; rm -rf /`` in an
argument is just text. ``run_command`` only runs commands from an allow-list
chosen by the developer, never anything the model makes up.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from agentforge.tools.base import BaseTool, ToolContext, ToolError, ToolPermission
from agentforge.tools.builtin.workspace import Workspace

MAX_CAPTURE_CHARS = 12_000
# Arguments the model may add: plain words, paths, options and node IDs such as
# "tests/test_x.py::test_y" or "-k=name". No spaces, quotes or shell syntax.
SAFE_ARG = re.compile(r"^[\w@%+=:,./\[\]-]{1,200}$")
GIT_REF = re.compile(r"^(?!-)[\w./~^@{}-]{1,100}$")


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def render(self) -> str:
        parts = [f"exit code: {self.exit_code}" + (" (timed out)" if self.timed_out else "")]
        if self.stdout.strip():
            parts.append(f"--- stdout ---\n{_tail(self.stdout)}")
        if self.stderr.strip():
            parts.append(f"--- stderr ---\n{_tail(self.stderr)}")
        return "\n".join(parts)


def _tail(text: str) -> str:
    """Keep the end of long output: test failures and errors are usually at the bottom."""
    if len(text) <= MAX_CAPTURE_CHARS:
        return text.rstrip()
    return (
        f"...[{len(text) - MAX_CAPTURE_CHARS} earlier characters omitted]\n"
        + text[-MAX_CAPTURE_CHARS:].rstrip()
    )


async def run_process(
    argv: Sequence[str],
    *,
    cwd: str,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run ``argv`` (no shell) and capture its output; kill it on timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Program not found: '{argv[0]}'.") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return ProcessResult(
            exit_code=-1,
            stdout=out.decode("utf-8", "replace"),
            stderr=err.decode("utf-8", "replace"),
            timed_out=True,
        )
    return ProcessResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", "replace"),
        stderr=err.decode("utf-8", "replace"),
    )


# --------------------------------------------------------------------------- git_diff


class GitDiffArgs(BaseModel):
    base: str = Field(
        default="HEAD",
        description="Commit, branch or range to diff against, e.g. 'HEAD', 'main', 'HEAD~3'",
    )
    staged: bool = Field(default=False, description="Show staged changes only")
    paths: list[str] = Field(default_factory=list, description="Limit to these workspace paths")
    context_lines: int = Field(default=3, ge=0, le=20)


class GitDiffTool(BaseTool[GitDiffArgs]):
    name = "git_diff"
    description = (
        "Show a unified git diff of the workspace against a commit or branch (default: "
        "uncommitted changes vs HEAD). Read-only."
    )
    args_model = GitDiffArgs
    permission = ToolPermission.READ
    timeout_s = 30.0

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace

    async def run(self, args: GitDiffArgs, ctx: ToolContext) -> str:
        if not GIT_REF.match(args.base):
            raise ToolError(f"'{args.base}' is not a valid git reference.")
        paths = [self.workspace.relative(self.workspace.resolve(p)) for p in args.paths]
        argv = [
            "git",
            "--no-pager",
            "diff",
            "--no-color",
            "--no-ext-diff",
            f"-U{args.context_lines}",
        ]
        argv += ["--cached", args.base] if args.staged else [args.base]
        argv += ["--", *paths] if paths else []
        result = await run_process(
            argv,
            cwd=str(self.workspace.root),
            timeout_s=self.timeout_s - 1,
            env={"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
        )
        if result.exit_code != 0:
            raise ToolError(f"git diff failed: {result.stderr.strip() or result.stdout.strip()}")
        diff = result.stdout.strip()
        return _tail(diff) if diff else "No changes."


# --------------------------------------------------------------------------- run_command


class RunCommandArgs(BaseModel):
    command: str = Field(description="Name of an allowed command, e.g. 'pytest'")
    args: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Extra arguments, e.g. ['tests/test_api.py', '-x']",
    )


class RunCommandTool(BaseTool[RunCommandArgs]):
    """Runs developer-approved commands only. The model picks a name; it never writes argv."""

    name = "run_command"
    description = "Run an allowed command in the workspace and return its exit code and output."
    args_model = RunCommandArgs
    permission = ToolPermission.EXEC
    timeout_s = 300.0

    def __init__(
        self,
        workspace: Workspace,
        allowed: Mapping[str, Sequence[str]],
        *,
        timeout_s: float | None = None,
    ) -> None:
        if not allowed:
            raise ValueError("RunCommandTool needs at least one allowed command")
        self.allowed = {name: list(argv) for name, argv in allowed.items()}
        if timeout_s is not None:
            self.timeout_s = timeout_s
        names = ", ".join(sorted(self.allowed))
        self.description = f"{type(self).description} Allowed commands: {names}."
        super().__init__()
        self.workspace = workspace

    async def run(self, args: RunCommandArgs, ctx: ToolContext) -> str:
        base = self.allowed.get(args.command)
        if base is None:
            raise ToolError(
                f"'{args.command}' is not allowed. Allowed: {', '.join(sorted(self.allowed))}."
            )
        for arg in args.args:
            if not SAFE_ARG.match(arg) or ".." in arg.split("/"):
                raise ToolError(f"Argument not allowed: '{arg}'.")
        result = await run_process(
            [*base, *args.args],
            cwd=str(self.workspace.root),
            timeout_s=max(self.timeout_s - 1, 0.5),
        )
        return result.render()
