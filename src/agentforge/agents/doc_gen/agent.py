"""Agent 4 — Documentation: docstrings and a usage guide for a module, checked against the code.

The model writes docstrings by name; AgentForge puts them into the source
(``docstrings.py``), so only docstring lines change, and proves it by comparing
the syntax trees. Before the result is accepted:

- every public definition must have a docstring,
- each new docstring must pass the rubric (parameters, raised exceptions,
  returns/yields described, summary line),
- ``>>>`` examples in docstrings must pass as doctests,
- Python code blocks in the usage guide must at least parse.

Problems go back to the model (up to two corrections).
"""

from __future__ import annotations

import ast
from pathlib import PurePosixPath
from typing import Any

from agentforge.agents.doc_gen.docstrings import (
    DocstringError,
    apply_docstrings,
    lint,
    placeable,
    python_blocks,
    source_lines,
)
from agentforge.agents.doc_gen.schema import DocGenInput, DocGenOutput
from agentforge.agents.test_gen.schema import import_name
from agentforge.core.agent import RunContext
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools import pytest_runner
from agentforge.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool, Workspace
from agentforge.tools.python_ast import (
    PythonSourceError,
    definitions,
    docstring_coverage,
    has_docstring,
    parse,
    same_code,
)
from agentforge.tools.registry import ToolRegistry

MAX_FILE_CHARS = 40_000
MAX_PROBLEMS = 15


@register_agent
class DocGenAgent(LLMAgent[DocGenInput, DocGenOutput]):
    name = "doc-gen"
    description = (
        "Writes docstrings (Google, NumPy or Sphinx style) and a usage guide for a Python "
        "module; checks they describe the code and that examples run, without changing code."
    )
    version = "0.1.0"
    tags = ("code", "documentation")
    input_model = DocGenInput
    output_model = DocGenOutput
    max_steps = 10
    max_tokens = 8192
    max_repairs = 2
    timeout_s = 300.0

    async def before_run(self, data: DocGenInput, ctx: RunContext) -> None:
        if data.repo_path:
            path = Workspace(data.repo_path).resolve(data.target)
            if not path.is_file():
                raise PythonSourceError(f"target '{data.target}' not found in {data.repo_path}")
            source = path.read_text(encoding="utf-8")
        else:
            source = data.files[data.target]
        tree = parse(source, data.target)
        lines = source_lines(source)
        ctx.metadata["source"] = source
        ctx.metadata["coverage_before"] = docstring_coverage(tree).percent
        ctx.metadata["wanted"] = [
            d.qualname
            for d in definitions(tree)
            if d.public
            and placeable(d, lines)
            and (data.rewrite_existing or not has_docstring(d.node))
        ]

    # ------------------------------------------------------------------ prompt

    def build_user_message(self, data: DocGenInput, ctx: RunContext) -> str:
        source: str = ctx.metadata["source"]
        wanted: list[str] = ctx.metadata["wanted"]
        parts = [
            f"Document `{data.target}` (import name `{import_name(data.target)}`) "
            f"in **{data.style.value}** docstring style.",
            "\nWrite docstrings for: " + (", ".join(wanted) or "(none: all are documented)"),
        ]
        if not data.rewrite_existing:
            parts.append("Leave existing docstrings alone.")
        parts.append(
            "Also write `readme`: a short Markdown usage guide with examples."
            if data.readme
            else "Set `readme` to null."
        )
        if data.instructions:
            parts.append(f"\n## Instructions from the user\n\n{data.instructions.strip()}")
        parts.append(f"\n## {data.target}\n\n```python\n{_clip(source).rstrip()}\n```")
        others = {p: c for p, c in data.files.items() if p != data.target}
        if others:
            parts.append("\n## Other files, for context")
            for path, content in sorted(others.items()):
                parts.append(f"\n### {path}\n\n```python\n{_clip(content).rstrip()}\n```")
        if data.repo_path:
            parts.append(
                "\nThe rest of the repository is available through your tools; read what the "
                "module uses if it helps you describe it. Paths are relative to the repo root."
            )
        return "\n".join(parts)

    def build_tools(self, data: DocGenInput, ctx: RunContext) -> ToolRegistry | None:
        if not data.repo_path:
            return None
        workspace = Workspace(data.repo_path)
        return ToolRegistry(
            [ReadFileTool(workspace), ListFilesTool(workspace), SearchTextTool(workspace)]
        )

    # ------------------------------------------------------------------ verification

    async def check(self, output: DocGenOutput, data: DocGenInput, ctx: RunContext) -> None:
        source: str = ctx.metadata["source"]
        names = [d.name for d in output.docstrings]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise OutputCheckError(f"Each name once, please. Repeated: {', '.join(duplicates)}")
        try:
            applied = apply_docstrings(
                source,
                {d.name: d.docstring for d in output.docstrings},
                rewrite_existing=data.rewrite_existing,
            )
            if not same_code(source, applied.source):  # pragma: no cover - guarded by design
                raise OutputCheckError("Adding the docstrings changed the code.")
            tree = parse(applied.source, data.target)
        except (DocstringError, PythonSourceError) as exc:
            raise OutputCheckError(f"The docstrings cannot be applied: {exc}") from exc

        problems: list[str] = []
        lines = source_lines(applied.source)
        missing = [
            d.qualname
            for d in definitions(tree)
            if d.public and placeable(d, lines) and not has_docstring(d.node)
        ]
        if missing:
            problems.append(f"still missing docstrings: {', '.join(missing)}")
        problems += lint(applied.source, list(applied.written), data.style)
        if data.readme and not (output.readme and output.readme.strip()):
            problems.append("readme is empty; write the usage guide")
        for i, block in enumerate(python_blocks(output.readme or ""), 1):
            try:
                ast.parse(block)
            except SyntaxError as exc:
                problems.append(f"readme code block {i} is not valid Python: {exc.msg}")
        if problems:
            shown = problems[:MAX_PROBLEMS]
            more = len(problems) - len(shown)
            raise OutputCheckError(
                "The documentation is incomplete:\n- "
                + "\n- ".join(shown)
                + (f"\n- ...and {more} more" if more > 0 else "")
            )

        ctx.metadata["documented"] = applied.source
        ctx.metadata["coverage_after"] = docstring_coverage(tree).percent
        new_docs = [d.docstring for d in output.docstrings if d.name in applied.written]
        if any(">>>" in doc for doc in new_docs) and pytest_runner.pytest_available():
            run = await self._doctests(data, applied.source)
            ctx.metadata["doctests"] = run
            if not run.passed:
                raise OutputCheckError(
                    "The >>> examples in the docstrings fail as doctests. Fix the examples "
                    f"(the expected output must match exactly).\n\npytest output:\n{run.output}"
                )

    def postprocess(self, output: DocGenOutput, data: DocGenInput, ctx: RunContext) -> DocGenOutput:
        update: dict[str, Any] = {
            "documented_source": ctx.metadata.get("documented", ctx.metadata["source"]),
            "coverage_before": ctx.metadata["coverage_before"],
            "coverage_after": ctx.metadata.get("coverage_after", ctx.metadata["coverage_before"]),
            "doctests": ctx.metadata.get("doctests"),
        }
        return output.model_copy(update=update)

    async def _doctests(self, data: DocGenInput, documented: str) -> pytest_runner.PytestRun:
        args = ["--doctest-modules"]
        if not data.repo_path:
            files = {**data.files, data.target: documented}
            return await pytest_runner.run_pytest(
                files=files, tests=[data.target], measure=None, args=args
            )
        # Repo mode: the documented copy plus its packages' __init__ files, so the copy
        # shadows the repository's version and relative imports still work.
        workspace = Workspace(data.repo_path)
        files = {data.target: documented}
        for parent in PurePosixPath(data.target).parents:
            init = (parent / "__init__.py").as_posix()
            if parent.name and (workspace.root / init).is_file():
                files[init] = (workspace.root / init).read_text(encoding="utf-8")
        return await pytest_runner.run_pytest(
            files=files,
            tests=[data.target],
            measure=None,
            extra_paths=[workspace.root, workspace.root / "src"],
            args=args,
        )


def _clip(text: str) -> str:
    return text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n# ...[truncated]"
