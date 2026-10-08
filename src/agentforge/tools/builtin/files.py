"""Read-only file tools, confined to a Workspace: read_file, list_files, search_text."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from pydantic import BaseModel, Field

from agentforge.tools.base import BaseTool, ToolContext, ToolError, ToolPermission
from agentforge.tools.builtin.workspace import Workspace

MAX_FILE_BYTES = 1_000_000


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path relative to the workspace root, e.g. 'src/app.py'")
    start_line: int = Field(default=1, ge=1, description="First line to return (1-based)")
    max_lines: int = Field(default=400, ge=1, le=2000, description="How many lines to return")


class ReadFileTool(BaseTool[ReadFileArgs]):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from the workspace. Lines are numbered so you can refer to "
        "them. Use start_line/max_lines to page through long files."
    )
    args_model = ReadFileArgs
    permission = ToolPermission.READ

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace

    async def run(self, args: ReadFileArgs, ctx: ToolContext) -> str:
        path = self.workspace.resolve(args.path)
        return await asyncio.to_thread(self._read, path, args)

    def _read(self, path: Path, args: ReadFileArgs) -> str:
        if not path.is_file():
            raise ToolError(f"File not found: '{args.path}'.")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"'{args.path}' is larger than {MAX_FILE_BYTES // 1000} KB.")
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            raise ToolError(f"'{args.path}' looks like a binary file.")
        lines = data.decode("utf-8", errors="replace").splitlines()
        if args.start_line > max(len(lines), 1):
            raise ToolError(f"'{args.path}' has only {len(lines)} lines.")
        first = args.start_line - 1
        chunk = lines[first : first + args.max_lines]
        width = len(str(first + len(chunk)))
        body = "\n".join(f"{n:>{width}} | {line}" for n, line in enumerate(chunk, start=first + 1))
        shown_to = first + len(chunk)
        header = f"{self.workspace.relative(path)} (lines {first + 1}-{shown_to} of {len(lines)})"
        more = f"\n... {len(lines) - shown_to} more lines" if shown_to < len(lines) else ""
        return f"{header}\n{body}{more}"


class ListFilesArgs(BaseModel):
    path: str = Field(default=".", description="Folder to list, relative to the workspace root")
    pattern: str = Field(default="*", description="Glob for file names, e.g. '*.py'")
    recursive: bool = Field(default=True, description="Include sub-folders")
    max_results: int = Field(default=200, ge=1, le=2000)


class ListFilesTool(BaseTool[ListFilesArgs]):
    name = "list_files"
    description = (
        "List files in a workspace folder, optionally filtered by a glob such as '*.py'. "
        "Dependency, cache and VCS folders are skipped."
    )
    args_model = ListFilesArgs
    permission = ToolPermission.READ

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace

    async def run(self, args: ListFilesArgs, ctx: ToolContext) -> str:
        folder = self.workspace.resolve(args.path)
        if not folder.is_dir():
            raise ToolError(f"Folder not found: '{args.path}'.")
        return await asyncio.to_thread(self._list, folder, args)

    def _list(self, folder: Path, args: ListFilesArgs) -> str:
        walker = folder.rglob(args.pattern) if args.recursive else folder.glob(args.pattern)
        found: list[str] = []
        truncated = False
        for path in sorted(walker):
            if not path.is_file() or self.workspace.is_ignored(path):
                continue
            if self.workspace.is_denied(path) or not path.resolve().is_relative_to(
                self.workspace.root
            ):
                continue
            if len(found) == args.max_results:
                truncated = True
                break
            found.append(self.workspace.relative(path))
        if not found:
            return f"No files matching '{args.pattern}' in '{args.path}'."
        suffix = f"\n... more files not shown (limit {args.max_results})" if truncated else ""
        return "\n".join(found) + suffix


class SearchTextArgs(BaseModel):
    query: str = Field(min_length=1, description="Text or regular expression to search for")
    path: str = Field(default=".", description="Folder to search, relative to the workspace")
    pattern: str = Field(default="*", description="Only search files matching this glob")
    regex: bool = Field(default=False, description="Treat query as a regular expression")
    max_results: int = Field(default=100, ge=1, le=1000)


class SearchTextTool(BaseTool[SearchTextArgs]):
    name = "search_text"
    description = (
        "Search text files in the workspace and return matching lines as 'path:line: text'. "
        "Case-insensitive."
    )
    args_model = SearchTextArgs
    permission = ToolPermission.READ

    def __init__(self, workspace: Workspace) -> None:
        super().__init__()
        self.workspace = workspace

    async def run(self, args: SearchTextArgs, ctx: ToolContext) -> str:
        folder = self.workspace.resolve(args.path)
        if not folder.is_dir():
            raise ToolError(f"Folder not found: '{args.path}'.")
        try:
            needle = re.compile(args.query if args.regex else re.escape(args.query), re.IGNORECASE)
        except re.error as exc:
            raise ToolError(f"Invalid regular expression: {exc}") from exc
        return await asyncio.to_thread(self._search, folder, needle, args)

    def _search(self, folder: Path, needle: re.Pattern[str], args: SearchTextArgs) -> str:
        hits: list[str] = []
        for path in sorted(folder.rglob(args.pattern)):
            if not path.is_file() or self.workspace.is_ignored(path):
                continue
            if self.workspace.is_denied(path) or path.stat().st_size > MAX_FILE_BYTES:
                continue
            if not path.resolve().is_relative_to(self.workspace.root):
                continue
            data = path.read_bytes()
            if b"\x00" in data[:8192]:
                continue
            for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if needle.search(line):
                    hits.append(f"{self.workspace.relative(path)}:{number}: {line.strip()[:200]}")
                    if len(hits) >= args.max_results:
                        return "\n".join(hits) + f"\n... stopped at {args.max_results} matches"
        return "\n".join(hits) if hits else f"No matches for '{args.query}'."
