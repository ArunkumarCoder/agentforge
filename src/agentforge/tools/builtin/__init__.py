"""Built-in tools: workspace files, git, allow-listed commands and safe HTTP."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agentforge.tools.builtin.files import ListFilesTool, ReadFileTool, SearchTextTool
from agentforge.tools.builtin.http import HttpFetchTool, html_to_text, is_public_address
from agentforge.tools.builtin.process import (
    GitDiffTool,
    Limits,
    ProcessResult,
    RunCommandTool,
    run_process,
    with_limits,
)
from agentforge.tools.builtin.workspace import Workspace, WorkspaceError
from agentforge.tools.registry import ToolRegistry


def workspace_tools(
    workspace: Workspace,
    *,
    commands: Mapping[str, Sequence[str]] | None = None,
    http: bool = False,
) -> ToolRegistry:
    """A ready-made registry: file tools + git_diff, plus run_command / http_fetch if asked."""
    registry = ToolRegistry(
        [
            ReadFileTool(workspace),
            ListFilesTool(workspace),
            SearchTextTool(workspace),
            GitDiffTool(workspace),
        ]
    )
    if commands:
        registry.register(RunCommandTool(workspace, commands))
    if http:
        registry.register(HttpFetchTool())
    return registry


__all__ = [
    "GitDiffTool",
    "HttpFetchTool",
    "Limits",
    "ListFilesTool",
    "ProcessResult",
    "ReadFileTool",
    "RunCommandTool",
    "SearchTextTool",
    "Workspace",
    "WorkspaceError",
    "html_to_text",
    "is_public_address",
    "run_process",
    "with_limits",
    "workspace_tools",
]
