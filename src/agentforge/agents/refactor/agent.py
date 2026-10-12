"""Agent 5 — Refactoring: simpler code, same behaviour, proven by the tests.

The model gets the module, the measured smells and the tests. AgentForge then
checks its answer against reality before accepting it:

- the tests pass on the **original** first (otherwise there is nothing to compare to),
- the new source parses and keeps every public name and signature,
- there are fewer smells and less total excess than before,
- the tests still pass on the new source.

Anything that fails goes back to the model (up to two corrections). With no
tests the agent refuses unless ``allow_untested`` is set, and then reports
``behaviour_verified = False``.
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from agentforge.agents.refactor.schema import RefactorInput, RefactorOutput
from agentforge.agents.refactor.smells import Smell, find_smells
from agentforge.agents.test_gen.schema import import_name, is_test_file
from agentforge.core.agent import RunContext
from agentforge.core.errors import AgentForgeError
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools import pytest_runner
from agentforge.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool, Workspace
from agentforge.tools.python_ast import PythonSourceError, api_changes, parse, public_api
from agentforge.tools.registry import ToolRegistry

MAX_FILE_CHARS = 40_000
MAX_TEST_FILES = 20
MAX_SHOWN = 10


class RefactorError(AgentForgeError):
    """The refactoring cannot start (no tests, failing tests, missing target)."""


@register_agent
class RefactorAgent(LLMAgent[RefactorInput, RefactorOutput]):
    name = "refactor"
    description = (
        "Removes code smells (long or complex functions, deep nesting, long parameter lists, "
        "duplication) from a module; keeps the public API and proves behaviour with the tests."
    )
    version = "0.1.0"
    tags = ("code", "refactoring")
    input_model = RefactorInput
    output_model = RefactorOutput
    max_steps = 10
    max_tokens = 8192
    max_repairs = 2
    timeout_s = 600.0

    # ------------------------------------------------------------------ setup

    async def before_run(self, data: RefactorInput, ctx: RunContext) -> None:
        source = self._read_source(data)
        tree = parse(source, data.target)  # a syntax error here is the user's, not the model's
        smells = find_smells(source, data.thresholds)
        tests = self._test_files(data)
        if not tests and not data.allow_untested and smells:
            raise RefactorError(
                "no tests found for this module. Refactoring without tests cannot be verified; "
                "pass test files, or set allow_untested to accept that risk"
            )
        ctx.metadata.update(
            source=source,
            api=public_api(tree),
            smells_before=smells,
            tests=tests,
            clean=not smells,
        )
        if tests and smells:
            before = await self._run_tests(data, source, tests)
            if not before.passed:
                raise RefactorError(
                    "the tests already fail on the original code, so behaviour cannot be "
                    f"compared. Fix them first.\n\n{before.output}"
                )
            ctx.metadata["tests_before"] = before

    async def execute(self, data: RefactorInput, ctx: RunContext) -> RefactorOutput:
        if ctx.metadata["clean"]:  # nothing to fix: skip the model
            source: str = ctx.metadata["source"]
            return RefactorOutput(
                summary="No smells above the thresholds; the module is left as it is.",
                changes=[],
                refactored_source=source,
                behaviour_verified=False,
            )
        return await super().execute(data, ctx)

    # ------------------------------------------------------------------ prompt

    def build_user_message(self, data: RefactorInput, ctx: RunContext) -> str:
        source: str = ctx.metadata["source"]
        smells: list[Smell] = ctx.metadata["smells_before"]
        t = data.thresholds
        parts = [
            f"Refactor `{data.target}` (import name `{import_name(data.target)}`).",
            "\n## Smells to fix\n\n" + "\n".join(f"- {s.message}" for s in smells),
            f"\nLimits: {t.max_lines} lines, complexity {t.max_complexity}, "
            f"{t.max_parameters} parameters, nesting {t.max_nesting}, "
            f"duplicate runs of {t.min_duplicate_statements}+ statements.",
        ]
        if data.instructions:
            parts.append(f"\n## Instructions from the user\n\n{data.instructions.strip()}")
        parts.append(f"\n## {data.target}\n\n```python\n{_clip(source).rstrip()}\n```")
        tests: dict[str, str] = ctx.metadata["tests"]
        if tests:
            parts.append("\n## Tests that must keep passing")
            for path, content in sorted(tests.items()):
                parts.append(f"\n### {path}\n\n```python\n{_clip(content).rstrip()}\n```")
        else:
            parts.append("\nThere are no tests, so be especially careful to keep behaviour.")
        if data.repo_path:
            parts.append(
                "\nThe rest of the repository is available through your tools. "
                "Paths are relative to the repo root."
            )
        return "\n".join(parts)

    def build_tools(self, data: RefactorInput, ctx: RunContext) -> ToolRegistry | None:
        if not data.repo_path:
            return None
        workspace = Workspace(data.repo_path)
        return ToolRegistry(
            [ReadFileTool(workspace), ListFilesTool(workspace), SearchTextTool(workspace)]
        )

    # ------------------------------------------------------------------ verification

    async def check(self, output: RefactorOutput, data: RefactorInput, ctx: RunContext) -> None:
        source: str = ctx.metadata["source"]
        new = output.refactored_source
        try:
            tree = parse(new, data.target)
        except PythonSourceError as exc:
            raise OutputCheckError(f"The refactored source is not valid Python: {exc}") from exc

        changes = api_changes(ctx.metadata["api"], public_api(tree))
        if changes:
            raise OutputCheckError(
                "The public API changed, which is not allowed: "
                + "; ".join(changes[:MAX_SHOWN])
                + ". Keep every public name and signature; only add private helpers."
            )
        if new.strip() == source.strip():
            raise OutputCheckError("The source is unchanged. Fix the smells listed.")

        before: list[Smell] = ctx.metadata["smells_before"]
        after = find_smells(new, data.thresholds)
        if len(after) >= len(before) or _excess(after) >= _excess(before):
            remaining = "\n".join(f"- {s.message}" for s in after[:MAX_SHOWN]) or "- (none)"
            raise OutputCheckError(
                f"The code is not simpler: {len(after)} smell(s) now, {len(before)} before "
                f"(total excess {_excess(after)} vs {_excess(before)}). Smells now:\n{remaining}"
            )
        ctx.metadata["smells_after"] = after

        tests: dict[str, str] = ctx.metadata["tests"]
        if tests:
            run = await self._run_tests(data, new, tests)
            ctx.metadata["tests_after"] = run
            if not run.passed:
                raise OutputCheckError(
                    "The tests fail on the refactored code, so behaviour changed. Keep the "
                    f"behaviour identical and fix the refactoring.\n\npytest output:\n{run.output}"
                )

    def postprocess(
        self, output: RefactorOutput, data: RefactorInput, ctx: RunContext
    ) -> RefactorOutput:
        source: str = ctx.metadata["source"]
        new = output.refactored_source
        diff = "".join(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{data.target}",
                tofile=f"b/{data.target}",
            )
        )
        before, after = ctx.metadata.get("tests_before"), ctx.metadata.get("tests_after")
        update: dict[str, Any] = {
            "diff": diff,
            "smells_before": ctx.metadata["smells_before"],
            "smells_after": ctx.metadata.get("smells_after", []),
            "api_preserved": True,
            "tests_before": before,
            "tests_after": after,
            "behaviour_verified": bool(before and after and before.passed and after.passed),
        }
        return output.model_copy(update=update)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _read_source(data: RefactorInput) -> str:
        if not data.repo_path:
            return data.files[data.target]
        path = Workspace(data.repo_path).resolve(data.target)
        if not path.is_file():
            raise RefactorError(f"target '{data.target}' not found in {data.repo_path}")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _test_files(data: RefactorInput) -> dict[str, str]:
        """Test files to run, with content (repo mode adds the ``conftest.py`` files they need)."""
        if not data.repo_path:
            return {p: c for p, c in data.files.items() if is_test_file(p)}
        workspace = Workspace(data.repo_path)
        wanted = list(data.tests)
        if not wanted:
            module = import_name(data.target)
            needles = {module, module.rsplit(".", 1)[-1]}
            for path in sorted(workspace.root.rglob("*.py")):
                rel = workspace.relative(path)
                if workspace.is_ignored(path) or not is_test_file(rel):
                    continue
                if any(n in path.read_text(encoding="utf-8", errors="replace") for n in needles):
                    wanted.append(rel)
                if len(wanted) >= MAX_TEST_FILES:
                    break
        found: dict[str, str] = {}
        for rel in wanted:
            path = workspace.resolve(rel)
            if not path.is_file():
                raise RefactorError(f"test file '{rel}' not found in {data.repo_path}")
            found[rel] = path.read_text(encoding="utf-8")
            for parent in Path(rel).parents:
                for name in ("conftest.py", "__init__.py"):
                    support = (parent / name).as_posix()
                    if parent.name and (workspace.root / support).is_file():
                        found.setdefault(support, (workspace.root / support).read_text("utf-8"))
        return found

    @staticmethod
    async def _run_tests(
        data: RefactorInput, source: str, tests: dict[str, str]
    ) -> pytest_runner.PytestRun:
        targets = [p for p in tests if is_test_file(p)]
        if not data.repo_path:
            return await pytest_runner.run_pytest(
                files={**data.files, data.target: source}, tests=targets, measure=None
            )
        root = Workspace(data.repo_path).root
        files = {**tests, **pytest_runner.shadow_files(root, data.target, source)}
        return await pytest_runner.run_pytest(
            files=files, tests=targets, measure=None, extra_paths=[root, root / "src"]
        )


def _excess(smells: list[Smell]) -> int:
    return sum(max(s.value - s.limit, 0) for s in smells)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n# ...[truncated]"
