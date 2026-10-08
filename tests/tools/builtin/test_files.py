"""read_file, list_files and search_text."""

from typing import Any

from agentforge.core import ToolCall
from agentforge.tools import BaseTool, ToolContext
from agentforge.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool, Workspace


async def call(tool: BaseTool[Any], ctx: ToolContext, **arguments: Any) -> tuple[str, bool]:
    result = await tool.invoke(ToolCall(id="c", name=tool.name, arguments=arguments), ctx)
    return result.content, result.is_error


class TestReadFile:
    async def test_numbered_lines(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, error = await call(ReadFileTool(workspace), ctx, path="src/app/util.py")
        assert not error
        assert text == "src/app/util.py (lines 1-2 of 2)\n1 | def add(a, b):\n2 |     return a + b"

    async def test_paging(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, _ = await call(
            ReadFileTool(workspace), ctx, path="src/app/main.py", start_line=4, max_lines=1
        )
        assert text.startswith("src/app/main.py (lines 4-4 of 5)")
        assert "4 | def handler(event):" in text
        assert text.endswith("... 1 more lines")

    async def test_start_beyond_end(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, error = await call(ReadFileTool(workspace), ctx, path="README.md", start_line=50)
        assert error
        assert "only 2 lines" in text

    async def test_missing_binary_and_secret(self, workspace: Workspace, ctx: ToolContext) -> None:
        tool = ReadFileTool(workspace)
        assert (await call(tool, ctx, path="nope.py")) == ("File not found: 'nope.py'.", True)
        text, error = await call(tool, ctx, path="logo.png")
        assert error and "binary" in text
        text, error = await call(tool, ctx, path=".env")
        assert error and "not allowed" in text
        assert "super-secret" not in text

    async def test_traversal_is_refused(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, error = await call(ReadFileTool(workspace), ctx, path="../outside.txt")
        assert error
        assert "outside the workspace" in text

    async def test_too_large(self, workspace: Workspace, ctx: ToolContext) -> None:
        (workspace.root / "big.log").write_text("x" * 1_100_000, encoding="utf-8")
        text, error = await call(ReadFileTool(workspace), ctx, path="big.log")
        assert error and "larger than" in text


class TestListFiles:
    async def test_lists_and_skips_noise_and_secrets(
        self, workspace: Workspace, ctx: ToolContext
    ) -> None:
        text, error = await call(ListFilesTool(workspace), ctx)
        assert not error
        assert text.splitlines() == ["README.md", "logo.png", "src/app/main.py", "src/app/util.py"]

    async def test_glob_and_folder(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, _ = await call(ListFilesTool(workspace), ctx, path="src", pattern="m*.py")
        assert text == "src/app/main.py"

    async def test_non_recursive(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, _ = await call(ListFilesTool(workspace), ctx, recursive=False)
        assert text.splitlines() == ["README.md", "logo.png"]

    async def test_limit_and_empty(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, _ = await call(ListFilesTool(workspace), ctx, max_results=1)
        assert text.startswith("README.md\n... more files not shown")
        text, _ = await call(ListFilesTool(workspace), ctx, pattern="*.rs")
        assert text == "No files matching '*.rs' in '.'."

    async def test_missing_folder(self, workspace: Workspace, ctx: ToolContext) -> None:
        text, error = await call(ListFilesTool(workspace), ctx, path="docs")
        assert error and "Folder not found" in text


class TestSearchText:
    async def test_finds_matches_with_locations(
        self, workspace: Workspace, ctx: ToolContext
    ) -> None:
        text, error = await call(SearchTextTool(workspace), ctx, query="RETURN")
        assert not error
        assert text.splitlines() == [
            "src/app/main.py:5: return os.environ['TOKEN']",
            "src/app/util.py:2: return a + b",
        ]

    async def test_never_searches_secrets_or_ignored_dirs(
        self, workspace: Workspace, ctx: ToolContext
    ) -> None:
        text, _ = await call(SearchTextTool(workspace), ctx, query="secret")
        assert text == "No matches for 'secret'."
        text, _ = await call(SearchTextTool(workspace), ctx, query="ignored = True")
        assert text.startswith("No matches")

    async def test_regex_glob_and_limit(self, workspace: Workspace, ctx: ToolContext) -> None:
        tool = SearchTextTool(workspace)
        text, _ = await call(tool, ctx, query=r"def \w+\(", regex=True, pattern="*.py")
        assert len(text.splitlines()) == 2
        text, _ = await call(tool, ctx, query="def", max_results=1)
        assert text.endswith("... stopped at 1 matches")

    async def test_bad_regex_and_missing_folder(
        self, workspace: Workspace, ctx: ToolContext
    ) -> None:
        tool = SearchTextTool(workspace)
        text, error = await call(tool, ctx, query="(", regex=True)
        assert error and "Invalid regular expression" in text
        text, error = await call(tool, ctx, query="x", path="nope")
        assert error and "Folder not found" in text


def test_workspace_tools_bundle(workspace: Workspace) -> None:
    from agentforge.tools import ToolPermission
    from agentforge.tools.builtin import workspace_tools

    basic = workspace_tools(workspace)
    assert basic.names == ["git_diff", "list_files", "read_file", "search_text"]
    assert all(t.permission is ToolPermission.READ for t in basic)
    full = workspace_tools(workspace, commands={"pytest": ["uv", "run", "pytest"]}, http=True)
    assert full.names == [
        "git_diff",
        "http_fetch",
        "list_files",
        "read_file",
        "run_command",
        "search_text",
    ]
