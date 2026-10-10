"""Agent 2 — Bug Fix: an error and the code in, root cause and a verified-to-apply patch out."""

from __future__ import annotations

from agentforge.agents.bug_fix.schema import BugFixInput, BugFixOutput
from agentforge.core.agent import RunContext
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool, Workspace
from agentforge.tools.builtin.workspace import WorkspaceError
from agentforge.tools.patching import PatchError, apply_patch, parse_patch
from agentforge.tools.registry import ToolRegistry

MAX_FILE_CHARS = 40_000


@register_agent
class BugFixAgent(LLMAgent[BugFixInput, BugFixOutput]):
    name = "bug-fix"
    description = (
        "Finds the root cause of an error (traceback + code or a repository) and proposes a "
        "minimal fix as a unified diff that is checked to apply cleanly."
    )
    version = "0.1.0"
    tags = ("code", "debugging")
    input_model = BugFixInput
    output_model = BugFixOutput
    max_steps = 10

    def build_user_message(self, data: BugFixInput, ctx: RunContext) -> str:
        parts = ["Fix this error.", f"\n## Error\n\n```\n{data.error.strip()}\n```"]
        if data.description:
            parts.append(f"\n## Context from the user\n\n{data.description.strip()}")
        if data.files:
            parts.append("\n## Files")
            for path, content in sorted(data.files.items()):
                body = content if len(content) <= MAX_FILE_CHARS else content[:MAX_FILE_CHARS]
                parts.append(f"\n### {path}\n\n```\n{body.rstrip()}\n```")
        else:
            parts.append(
                "\nThe code is in the repository available through your tools. Read the files "
                "in the traceback before you answer; paths are relative to the repository root."
            )
        return "\n".join(parts)

    def build_tools(self, data: BugFixInput, ctx: RunContext) -> ToolRegistry | None:
        if not data.repo_path:
            return None
        workspace = Workspace(data.repo_path)
        return ToolRegistry(
            [ReadFileTool(workspace), ListFilesTool(workspace), SearchTextTool(workspace)]
        )

    async def check(self, output: BugFixOutput, data: BugFixInput, ctx: RunContext) -> None:
        """The patch must apply to the real files, or the model gets one chance to fix it."""
        try:
            sources = data.files or self._read_from_repo(data, output.patch)
            patched = apply_patch(sources, output.patch)
        except PatchError as exc:
            raise OutputCheckError(f"The patch does not apply: {exc}") from exc
        if all(patched[path] == sources.get(path) for path in patched):
            raise OutputCheckError("The patch does not change anything.")
        ctx.metadata["patched_files"] = patched

    def postprocess(self, output: BugFixOutput, data: BugFixInput, ctx: RunContext) -> BugFixOutput:
        return output.model_copy(update={"patch_applies": "patched_files" in ctx.metadata})

    @staticmethod
    def _read_from_repo(data: BugFixInput, patch: str) -> dict[str, str]:
        if data.repo_path is None:  # BugFixInput guarantees one source; keeps mypy honest
            raise PatchError("no files or repository to apply the patch to")
        workspace = Workspace(data.repo_path)
        files: dict[str, str] = {}
        for file_patch in parse_patch(patch):
            if file_patch.old_path is None:
                continue
            try:
                path = workspace.resolve(file_patch.old_path)
            except WorkspaceError as exc:
                raise PatchError(str(exc)) from exc
            if path.is_file():
                files[file_patch.old_path] = path.read_text(encoding="utf-8")
        return files
