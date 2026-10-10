"""Agent 3 — Test Generation: a module in, a pytest file that has actually passed out.

The model writes the tests; AgentForge runs them (see ``tools/pytest_runner.py``). Failing
tests go back to the model with pytest's output, up to ``max_attempts`` runs in
total. The result records how the final run went and how much of the target
the tests cover, next to the coverage from any existing tests.
"""

from __future__ import annotations

from typing import Any, ClassVar

from agentforge.agents.test_gen.schema import (
    GenerateTestsInput,
    GenerateTestsOutput,
    PytestRun,
    import_name,
    is_test_file,
)
from agentforge.core.agent import RunContext
from agentforge.core.errors import AgentForgeError
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools import pytest_runner as runner
from agentforge.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool, Workspace
from agentforge.tools.registry import ToolRegistry

MAX_FILE_CHARS = 40_000


class CannotRunTestsError(AgentForgeError):
    """The tests cannot be run at all (e.g. pytest is not installed)."""


@register_agent
class TestGenAgent(LLMAgent[GenerateTestsInput, GenerateTestsOutput]):
    __test__: ClassVar[bool] = False  # not a pytest test class

    name = "test-gen"
    description = (
        "Writes a pytest file for a Python module, runs it, and fixes failing tests until "
        "they pass; reports coverage of the module before and after."
    )
    version = "0.1.0"
    tags = ("code", "testing")
    input_model = GenerateTestsInput
    output_model = GenerateTestsOutput
    max_steps = 10
    max_tokens = 8192
    timeout_s = 600.0

    def repair_budget(self, data: GenerateTestsInput) -> int:
        return data.max_attempts - 1

    async def before_run(self, data: GenerateTestsInput, ctx: RunContext) -> None:
        if not runner.pytest_available():
            raise CannotRunTestsError(
                "pytest is not installed in AgentForge's environment, so tests cannot be run"
            )
        if data.repo_path and not Workspace(data.repo_path).resolve(data.target).is_file():
            raise CannotRunTestsError(f"target '{data.target}' not found in {data.repo_path}")
        ctx.metadata["test_attempts"] = 0

    # ------------------------------------------------------------------ prompt

    def build_user_message(self, data: GenerateTestsInput, ctx: RunContext) -> str:
        module = import_name(data.target)
        parts = [
            f"Write pytest tests for `{data.target}` (import it as `{module}`).",
        ]
        if data.instructions:
            parts.append(f"\n## Instructions from the user\n\n{data.instructions.strip()}")
        if data.files:
            existing = sorted(p for p in data.files if is_test_file(p))
            if existing:
                parts.append(
                    "\nExisting tests (cover what they miss; do not reuse their path): "
                    + ", ".join(existing)
                )
            parts.append("\n## Files")
            for path, content in sorted(data.files.items()):
                parts.append(f"\n### {path}\n\n```python\n{_clip(content).rstrip()}\n```")
        else:
            source = Workspace(str(data.repo_path)).resolve(data.target).read_text(encoding="utf-8")
            parts.append(f"\n## {data.target}\n\n```python\n{_clip(source).rstrip()}\n```")
            parts.append(
                "\nThe rest of the repository is available through your tools; read what the "
                "target imports if you need it. Paths are relative to the repository root."
            )
        return "\n".join(parts)

    def build_tools(self, data: GenerateTestsInput, ctx: RunContext) -> ToolRegistry | None:
        if not data.repo_path:
            return None
        workspace = Workspace(data.repo_path)
        return ToolRegistry(
            [ReadFileTool(workspace), ListFilesTool(workspace), SearchTextTool(workspace)]
        )

    # ------------------------------------------------------------------ verification

    async def check(
        self, output: GenerateTestsOutput, data: GenerateTestsInput, ctx: RunContext
    ) -> None:
        """Run the tests; failing tests go back to the model with pytest's output."""
        if output.test_path in data.files:
            raise OutputCheckError(
                f"test_path '{output.test_path}' already exists; pick a new file name"
            )
        if "coverage_before" not in ctx.metadata:
            ctx.metadata["coverage_before"] = await self._coverage_before(data)

        run = await self._run(data, {output.test_path: output.test_code}, [output.test_path])
        ctx.metadata["test_attempts"] += 1
        ctx.metadata["pytest_run"] = run
        ctx.log.info(
            "testgen.run",
            attempt=ctx.metadata["test_attempts"],
            passed=run.passed,
            tests=run.tests,
            failures=run.failures,
            coverage=run.coverage,
        )
        if run.passed:
            return
        if run.tests == 0 and run.failures == 0:
            problem = "pytest collected no tests"
        else:
            problem = f"{run.failures} of {run.tests} tests failed or errored"
        raise OutputCheckError(
            f"The tests did not pass: {problem}. Fix the tests, not the module (use a strict "
            "xfail for a real bug). Return the complete corrected file.\n\n"
            f"pytest output:\n{run.output}"
        )

    def postprocess(
        self, output: GenerateTestsOutput, data: GenerateTestsInput, ctx: RunContext
    ) -> GenerateTestsOutput:
        update: dict[str, Any] = {
            "run": ctx.metadata.get("pytest_run"),
            "coverage_before": ctx.metadata.get("coverage_before"),
            "attempts": ctx.metadata.get("test_attempts", 0),
        }
        return output.model_copy(update=update)

    async def _coverage_before(self, data: GenerateTestsInput) -> float | None:
        """Coverage of the target by the tests passed in ``files``; None in repo mode."""
        if data.repo_path:
            return None
        existing = sorted(p for p in data.files if is_test_file(p))
        if not existing:
            return 0.0 if runner.coverage_available() else None
        return (await self._run(data, {}, existing)).coverage

    async def _run(
        self, data: GenerateTestsInput, new: dict[str, str], tests: list[str]
    ) -> PytestRun:
        if data.repo_path:
            workspace = Workspace(data.repo_path)
            root = workspace.root
            return await runner.run_pytest(
                files=new,
                tests=tests,
                measure=workspace.resolve(data.target),
                extra_paths=[root, root / "src"],
            )
        return await runner.run_pytest(
            files={**data.files, **new}, tests=tests, measure=data.target
        )


def _clip(text: str) -> str:
    return text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n# ...[truncated]"
