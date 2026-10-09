"""Agent 1 — Code Review: a unified diff in, prioritised findings and a verdict out."""

from __future__ import annotations

from agentforge.agents.code_review.diff import ChangedFile, diff_stats, parse_unified_diff
from agentforge.agents.code_review.schema import (
    Category,
    Finding,
    ReviewInput,
    ReviewOutput,
    Severity,
    Verdict,
)
from agentforge.core.agent import RunContext
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.schemas import ToolCall
from agentforge.tools.base import ToolContext, ToolError
from agentforge.tools.builtin import GitDiffTool, ReadFileTool, SearchTextTool, Workspace
from agentforge.tools.registry import ToolRegistry

MAX_DIFF_CHARS = 60_000
BLOCKING = {Severity.CRITICAL, Severity.MAJOR}


@register_agent
class CodeReviewAgent(LLMAgent[ReviewInput, ReviewOutput]):
    name = "code-review"
    description = (
        "Reviews a code change (unified diff or a repo's uncommitted changes) and returns "
        "prioritised findings with severity, location and a suggested fix, plus a verdict."
    )
    version = "0.1.0"
    tags = ("code", "quality", "security")
    input_model = ReviewInput
    output_model = ReviewOutput
    max_steps = 6

    async def execute(self, data: ReviewInput, ctx: RunContext) -> ReviewOutput:
        diff = data.diff if data.diff else await self._read_repo_diff(data, ctx)
        if not diff.strip() or diff.strip() == "No changes.":
            return ReviewOutput(
                summary="There are no changes to review.", verdict=Verdict.APPROVE, findings=[]
            )
        files = parse_unified_diff(diff)
        ctx.metadata["diff"] = diff
        ctx.metadata["changed_files"] = files
        ctx.log.info("review.diff", stats=diff_stats(files))
        return await super().execute(data, ctx)

    def build_user_message(self, data: ReviewInput, ctx: RunContext) -> str:
        diff: str = ctx.metadata["diff"]
        truncated = ""
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS]
            truncated = "\n\n(The diff was truncated; review what is shown.)"
        focus = ", ".join(c.value for c in data.focus) if data.focus else "all categories"
        return (
            f"Review this change. Focus: {focus}. Report at most {data.max_findings} findings, "
            f"most important first.\n\n```diff\n{diff}\n```{truncated}"
        )

    def build_tools(self, data: ReviewInput, ctx: RunContext) -> ToolRegistry | None:
        if not data.repo_path:
            return None  # a pasted diff has no surrounding code to read
        workspace = Workspace(data.repo_path)
        return ToolRegistry([ReadFileTool(workspace), SearchTextTool(workspace)])

    def postprocess(self, output: ReviewOutput, data: ReviewInput, ctx: RunContext) -> ReviewOutput:
        files: dict[str, ChangedFile] = ctx.metadata["changed_files"]
        kept: list[Finding] = []
        dropped = 0
        for finding in output.findings:
            changed = files.get(finding.file.removeprefix("a/").removeprefix("b/"))
            if changed is None:
                dropped += 1  # the model named a file that is not part of this change
                continue
            update: dict[str, object] = {"file": changed.path}
            if finding.line is not None and finding.line not in changed.hunk_lines:
                update["line"] = None  # don't point readers at a line we can't verify
            if (
                data.focus
                and finding.category not in data.focus
                and finding.severity not in BLOCKING
            ):
                continue
            kept.append(finding.model_copy(update=update))
        if dropped:
            ctx.log.warning("review.findings.dropped", count=dropped, reason="file not in diff")

        kept.sort(key=lambda f: (f.severity.rank, f.file, f.line or 0))
        kept = kept[: data.max_findings]
        verdict = _consistent_verdict(output.verdict, kept)
        if verdict is not output.verdict:
            ctx.log.info("review.verdict.adjusted", model=output.verdict, final=verdict)
        return output.model_copy(update={"findings": kept, "verdict": verdict})

    async def _read_repo_diff(self, data: ReviewInput, ctx: RunContext) -> str:
        if data.repo_path is None:  # pragma: no cover - ReviewInput guarantees one source
            raise ToolError("no repo_path given")
        tool = GitDiffTool(Workspace(data.repo_path))
        result = await tool.invoke(
            ToolCall(id="pre-review-diff", name=tool.name, arguments={"base": data.base}),
            ToolContext(run_id=ctx.run_id, correlation_id=ctx.correlation_id, log=ctx.log),
        )
        if result.is_error:
            raise ToolError(result.content)
        return result.content


def _consistent_verdict(proposed: Verdict, findings: list[Finding]) -> Verdict:
    """The verdict must follow from the findings, whatever the model said."""
    if any(f.severity in BLOCKING for f in findings):
        return Verdict.REQUEST_CHANGES
    if proposed is Verdict.REQUEST_CHANGES:
        return Verdict.COMMENT if findings else Verdict.APPROVE
    return proposed


__all__ = ["Category", "CodeReviewAgent", "ReviewInput", "ReviewOutput", "Severity", "Verdict"]
