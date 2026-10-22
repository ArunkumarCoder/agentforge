"""Running programs safely: a shared subprocess helper, git_diff and run_command.

No shell is ever used: commands are argument lists, so ``; rm -rf /`` in an
argument is just text. ``run_command`` only runs commands from an allow-list
chosen by the developer, never anything the model makes up.

``run_process`` is also where code written by a model gets run (generated tests, profile
scripts, SQLAlchemy models), so it is hardened, though none of this is a container:

- the child starts in its own process group, and a timeout or an output overflow kills the
  whole group, so grandchildren (a test that spawns a server) do not outlive the run,
- captured output is capped (8 MB by default), and a program that prints more is killed,
- with ``limits`` (POSIX only) the child gets CPU-time, memory and file-size limits set by a
  small launcher just before it ``exec``s the real program. On Windows limits are ignored.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from agentforge.tools.base import BaseTool, ToolContext, ToolError, ToolPermission
from agentforge.tools.builtin.workspace import Workspace

MAX_CAPTURE_CHARS = 12_000
# Arguments the model may add: plain words, paths, options and node IDs such as
# "tests/test_x.py::test_y" or "-k=name". No spaces, quotes or shell syntax.
SAFE_ARG = re.compile(r"^[\w@%+=:,./\[\]-]{1,200}$")
GIT_REF = re.compile(r"^(?!-)[\w./~^@{}-]{1,100}$")


DEFAULT_OUTPUT_MB = 8
LAUNCHER_NOT_FOUND = "agentforge-launcher: program not found"
_MB = 1024 * 1024

# Runs inside a fresh interpreter: lower the limits, then become the real program.
_LAUNCHER = f"""\
import json, os, resource, sys
for name, value in json.loads(sys.argv[1]).items():
    which = getattr(resource, name)
    soft, hard = resource.getrlimit(which)
    if hard != resource.RLIM_INFINITY:
        value = min(value, hard)
    top = value + 5 if name == "RLIMIT_CPU" else value  # CPU: SIGXCPU first, SIGKILL 5 s later
    if hard != resource.RLIM_INFINITY:
        top = min(top, hard)
    try:
        resource.setrlimit(which, (value, top))
    except (ValueError, OSError):
        pass
try:
    os.execvp(sys.argv[2], sys.argv[2:])
except FileNotFoundError:
    sys.stderr.write({LAUNCHER_NOT_FOUND!r} + ": " + sys.argv[2] + "\\n")
    sys.exit(127)
"""


@dataclass(frozen=True)
class Limits:
    """Resource limits for a child process. ``None`` leaves that limit alone."""

    cpu_s: int | None = None  # CPU seconds (the process gets SIGXCPU, then dies)
    memory_mb: int | None = None  # address space; allocations beyond it fail
    file_mb: int | None = None  # largest single file it may write
    output_mb: int = DEFAULT_OUTPUT_MB  # captured stdout and stderr, each

    def rlimits(self) -> dict[str, int]:
        """The ``resource`` limits to set, by ``RLIMIT_*`` name; always no core dumps."""
        limits = {"RLIMIT_CORE": 0}
        if self.cpu_s is not None:
            limits["RLIMIT_CPU"] = self.cpu_s
        if self.memory_mb is not None:
            limits["RLIMIT_AS"] = self.memory_mb * _MB
        if self.file_mb is not None:
            limits["RLIMIT_FSIZE"] = self.file_mb * _MB
        return limits


def with_limits(argv: Sequence[str], limits: Limits | None) -> list[str]:
    """``argv`` prefixed with the launcher that applies ``limits`` (POSIX); unchanged otherwise.

    For callers that start their own process (``subprocess.run``) but want the same limits.
    """
    if limits is not None and sys.platform != "win32":
        return [sys.executable, "-I", "-c", _LAUNCHER, json.dumps(limits.rlimits()), *argv]
    return list(argv)


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_truncated: bool = False  # it printed more than the cap and was killed

    @property
    def signal_name(self) -> str | None:
        """``SIGXCPU``, ``SIGKILL`` ... when a signal ended the process (POSIX)."""
        if self.exit_code < 0:
            try:
                return signal.Signals(-self.exit_code).name
            except ValueError:
                return f"signal {-self.exit_code}"
        return None

    def render(self) -> str:
        note = ""
        if self.timed_out:
            note = " (timed out)"
        elif self.output_truncated:
            note = " (stopped: too much output)"
        elif self.signal_name:
            note = f" (killed by {self.signal_name})"
        parts = [f"exit code: {self.exit_code}{note}"]
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


@dataclass
class _Capture:
    """One output stream, kept up to a cap; the rest is counted and dropped."""

    cap: int
    data: bytearray = field(default_factory=bytearray)
    overflowed: bool = False

    def add(self, chunk: bytes) -> None:
        room = self.cap - len(self.data)
        if room > 0:
            self.data += chunk[:room]
        if len(chunk) > room:
            self.overflowed = True


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the child and, on POSIX, everything it started (it leads its own group)."""
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
            return
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def _drain(
    stream: asyncio.StreamReader | None, capture: _Capture, proc: asyncio.subprocess.Process
) -> None:
    if stream is None:
        return
    while chunk := await stream.read(65536):
        capture.add(chunk)
        if capture.overflowed:
            _kill_group(proc)
            return


async def run_process(
    argv: Sequence[str],
    *,
    cwd: str,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    inherit_env: bool = True,
    limits: Limits | None = None,
) -> ProcessResult:
    """Run ``argv`` (no shell) and capture its output; kill it (and its children) on timeout.

    ``env`` is added to this process's environment, or, with ``inherit_env=False``,
    is the whole environment (use that to keep secrets such as API keys out).
    ``limits`` caps CPU time, memory, file size and captured output (see :class:`Limits`).
    """
    base = dict(os.environ) if inherit_env else {}
    command = with_limits(argv, limits)
    posix = sys.platform != "win32"
    cap = (limits.output_mb if limits else DEFAULT_OUTPUT_MB) * _MB
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env={**base, **(env or {})},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=posix,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Program not found: '{argv[0]}'.") from exc
    out, err = _Capture(cap), _Capture(cap)
    readers = asyncio.gather(_drain(proc.stdout, out, proc), _drain(proc.stderr, err, proc))
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.gather(readers, proc.wait()), timeout_s)
    except TimeoutError:
        timed_out = True
        _kill_group(proc)
        await proc.wait()
    exit_code = proc.returncode if proc.returncode is not None else -1
    stderr = err.data.decode("utf-8", "replace")
    if limits is not None and exit_code == 127 and LAUNCHER_NOT_FOUND in stderr:
        raise ToolError(f"Program not found: '{argv[0]}'.")
    return ProcessResult(
        exit_code=-1 if timed_out else exit_code,
        stdout=out.data.decode("utf-8", "replace"),
        stderr=stderr,
        timed_out=timed_out,
        output_truncated=out.overflowed or err.overflowed,
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
