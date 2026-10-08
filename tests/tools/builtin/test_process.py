"""git_diff and run_command: real subprocesses, no shell."""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentforge.core import ToolCall
from agentforge.tools import BaseTool, ToolContext
from agentforge.tools.builtin import GitDiffTool, RunCommandTool, Workspace, run_process

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


async def call(tool: BaseTool[Any], ctx: ToolContext, **arguments: Any) -> tuple[str, bool]:
    result = await tool.invoke(ToolCall(id="c", name=tool.name, arguments=arguments), ctx)
    return result.content, result.is_error


def git(root: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed git commands in tests
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(project: Path) -> Path:
    git(project, "init", "-q")
    git(project, "add", "src", "README.md")
    git(project, "commit", "-q", "-m", "initial")
    (project / "src" / "app" / "util.py").write_text(
        "def add(a, b):\n    return a + b + 0\n", encoding="utf-8"
    )
    return project


@needs_git
class TestGitDiff:
    async def test_uncommitted_changes(self, repo: Path, ctx: ToolContext) -> None:
        text, error = await call(GitDiffTool(Workspace(repo)), ctx)
        assert not error
        assert "diff --git a/src/app/util.py b/src/app/util.py" in text
        assert "-    return a + b" in text
        assert "+    return a + b + 0" in text

    async def test_staged_and_paths(self, repo: Path, ctx: ToolContext) -> None:
        tool = GitDiffTool(Workspace(repo))
        assert (await call(tool, ctx, staged=True))[0] == "No changes."
        assert (await call(tool, ctx, paths=["README.md"]))[0] == "No changes."
        text, _ = await call(tool, ctx, paths=["src/app/util.py"], context_lines=0)
        assert "+    return a + b + 0" in text

    @pytest.mark.parametrize("base", ["--output=/tmp/x", "-p", "HEAD; rm -rf /", "a b"])
    async def test_option_injection_is_refused(
        self, repo: Path, ctx: ToolContext, base: str
    ) -> None:
        text, error = await call(GitDiffTool(Workspace(repo)), ctx, base=base)
        assert error
        assert "not a valid git reference" in text

    async def test_paths_must_stay_in_workspace(self, repo: Path, ctx: ToolContext) -> None:
        text, error = await call(GitDiffTool(Workspace(repo)), ctx, paths=["../outside.txt"])
        assert error and "outside the workspace" in text

    async def test_unknown_ref(self, repo: Path, ctx: ToolContext) -> None:
        text, error = await call(GitDiffTool(Workspace(repo)), ctx, base="no-such-branch")
        assert error and "git diff failed" in text


PY = sys.executable
COMMANDS = {
    "hello": [PY, "-c", "import sys; print('hello', *sys.argv[1:])"],
    "fail": [PY, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"],
    "slow": [PY, "-c", "import time; time.sleep(5)"],
    "noisy": [PY, "-c", "print('x' * 20000)"],
}


class TestRunCommand:
    async def test_allowed_command_with_args(self, workspace: Workspace, ctx: ToolContext) -> None:
        tool = RunCommandTool(workspace, COMMANDS)
        text, error = await call(
            tool, ctx, command="hello", args=["tests/test_x.py::test_y", "-k=fast"]
        )
        assert not error
        assert text == "exit code: 0\n--- stdout ---\nhello tests/test_x.py::test_y -k=fast"

    async def test_failure_shows_exit_code_and_stderr(
        self, workspace: Workspace, ctx: ToolContext
    ) -> None:
        text, error = await call(RunCommandTool(workspace, COMMANDS), ctx, command="fail")
        assert not error  # a failing test run is a valid result for the model to read
        assert text == "exit code: 3\n--- stderr ---\nboom"

    async def test_unknown_command(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, error = await call(RunCommandTool(workspace, COMMANDS), ctx, command="rm")
        assert error
        assert "'rm' is not allowed. Allowed: fail, hello, noisy, slow." in text

    @pytest.mark.parametrize(
        "arg", ["; rm -rf /", "$(whoami)", "a b", "`id`", "../secrets", "x|y", "--x='1'"]
    )
    async def test_unsafe_arguments(self, workspace: Workspace, ctx: ToolContext, arg: str) -> None:
        text, error = await call(
            RunCommandTool(workspace, COMMANDS), ctx, command="hello", args=[arg]
        )
        assert error
        assert "Argument not allowed" in text

    async def test_timeout_kills_the_process(self, workspace: Workspace, ctx: ToolContext) -> None:
        tool = RunCommandTool(workspace, COMMANDS, timeout_s=1.5)
        text, error = await call(tool, ctx, command="slow")
        assert not error
        assert text.startswith("exit code: -1 (timed out)")

    async def test_long_output_keeps_the_tail(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, _ = await call(RunCommandTool(workspace, COMMANDS), ctx, command="noisy")
        assert "earlier characters omitted" in text

    async def test_needs_exec_permission(self, workspace: Workspace) -> None:
        text, error = await call(
            RunCommandTool(workspace, COMMANDS), ToolContext(), command="hello"
        )
        assert error and "Permission denied" in text

    def test_description_lists_commands_and_needs_one(self, workspace: Workspace) -> None:
        assert (
            "Allowed commands: fail, hello, noisy, slow."
            in RunCommandTool(workspace, COMMANDS).description
        )
        with pytest.raises(ValueError, match="at least one"):
            RunCommandTool(workspace, {})


async def test_missing_program(tmp_path: Path) -> None:
    from agentforge.tools import ToolError

    with pytest.raises(ToolError, match="Program not found"):
        await run_process(["definitely-not-a-real-program-xyz"], cwd=str(tmp_path), timeout_s=5)
