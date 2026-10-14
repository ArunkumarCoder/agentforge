"""Agent 10 — Migration: bring code up to Python 3.12 and prove nothing broke.

A scanner (``tools/py_migrate.py``) lists every deprecated or removed API in the code.
The model plans the migration and returns the rewritten files. AgentForge then checks
the result against reality before accepting it (up to two corrections):

- every returned file exists, is valid Python and really changed,
- the scanner finds **no remaining site** except those the model skipped with a reason,
  and no new kind of site appeared,
- the public API (names and signatures) is unchanged,
- the plan covers every changed file and refers to real site ids,
- the tests pass on the migrated code (they may already fail before, on APIs that no
  longer exist: that is often why you are migrating).

Code is parsed, never executed, except by the tests you supply.
"""

from __future__ import annotations

import ast
import difflib
from pathlib import Path

from agentforge.agents.migration.schema import (
    MigrationInput,
    MigrationOutput,
    SiteInfo,
    Skip,
    Step,
)
from agentforge.agents.test_gen.schema import is_test_file
from agentforge.core.agent import RunContext
from agentforge.core.errors import AgentForgeError
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools import py_migrate, pytest_runner
from agentforge.tools.builtin import ListFilesTool, ReadFileTool, SearchTextTool, Workspace
from agentforge.tools.py_migrate import Site
from agentforge.tools.python_ast import api_changes, parse, public_api
from agentforge.tools.registry import ToolRegistry

MAX_FILE_CHARS = 40_000
MAX_FILES = 200
MAX_FILE_BYTES = 200_000
MAX_SITES = 120
MAX_TEST_FILES = 30
MAX_SHOWN = 8


class MigrationError(AgentForgeError):
    """The migration cannot start (no files, too many sites)."""


@register_agent
class MigrationAgent(LLMAgent[MigrationInput, MigrationOutput]):
    name = "migration"
    description = (
        "Migrates Python code to 3.12: plans the steps, rewrites deprecated and removed APIs "
        "(typing generics, utcnow, collections ABCs, unittest aliases, removed modules), and "
        "proves the sites are gone, the API is kept and the tests pass."
    )
    version = "0.1.0"
    tags = ("code", "migration")
    input_model = MigrationInput
    output_model = MigrationOutput
    max_steps = 10
    max_tokens = 16384
    max_repairs = 2
    timeout_s = 900.0

    # ------------------------------------------------------------------ setup

    async def before_run(self, data: MigrationInput, ctx: RunContext) -> None:
        files = self._collect(data)
        if not files:
            raise MigrationError("no readable Python files to migrate")
        sites = py_migrate.scan(files, data.rules or None)
        if len(sites) > MAX_SITES:
            raise MigrationError(
                f"{len(sites)} migration sites is too many to handle at once; narrow the "
                "migration with 'paths' or 'rules'"
            )
        tests = self._test_paths(data, files)
        ctx.metadata.update(
            files=files,
            sites={f"M{i}": s for i, s in enumerate(sites, start=1)},
            tests=tests,
        )
        if tests and sites:
            ctx.metadata["tests_before"] = await self._run_tests(data, files, set(), tests)

    async def execute(self, data: MigrationInput, ctx: RunContext) -> MigrationOutput:
        if not ctx.metadata["sites"]:  # nothing to migrate: skip the model
            return MigrationOutput(
                summary="No deprecated or removed APIs found; the code is left as it is.",
                compiled=True,
            )
        return await super().execute(data, ctx)

    # ------------------------------------------------------------------ prompt

    def build_user_message(self, data: MigrationInput, ctx: RunContext) -> str:
        files: dict[str, str] = ctx.metadata["files"]
        sites: dict[str, Site] = ctx.metadata["sites"]
        before = ctx.metadata.get("tests_before")
        parts = ["Migrate this code to Python 3.12.", "\n## Sites to migrate\n"]
        parts.extend(
            f"- {sid}: [{s.rule}] {s.file}:{s.line} `{s.code}` ({s.detail})"
            for sid, s in sites.items()
        )
        parts.append("\n## Rule hints\n")
        parts.extend(
            f"- {rule}: {py_migrate.HINTS[rule]}"
            for rule in sorted({s.rule for s in sites.values()})
        )
        if data.instructions:
            parts.append(f"\n## Instructions from the user\n\n{data.instructions.strip()}")
        if before is not None:
            state = "pass" if before.passed else f"FAIL ({before.failures} failing)"
            parts.append(f"\nThe tests currently {state} on the original code.")
        for path, content in sorted(files.items()):
            parts.append(f"\n## {path}\n\n```python\n{_clip(content).rstrip()}\n```")
        if data.repo_path:
            parts.append(
                "\nThe rest of the repository is available through your tools. "
                "Paths are relative to the repo root."
            )
        return "\n".join(parts)

    def build_tools(self, data: MigrationInput, ctx: RunContext) -> ToolRegistry | None:
        if not data.repo_path:
            return None
        workspace = Workspace(data.repo_path)
        return ToolRegistry(
            [ReadFileTool(workspace), ListFilesTool(workspace), SearchTextTool(workspace)]
        )

    # ------------------------------------------------------------------ verification

    async def check(self, output: MigrationOutput, data: MigrationInput, ctx: RunContext) -> None:
        files: dict[str, str] = ctx.metadata["files"]
        sites: dict[str, Site] = ctx.metadata["sites"]
        everything_skipped = {k.site_id for k in output.skipped} >= set(sites)
        problems = _file_problems(output.files, files, everything_skipped)
        problems.extend(_skip_problems(output.skipped, sites))
        if problems:
            raise OutputCheckError(_report(problems))

        new = {**files, **output.files}
        problems = _leftover_problems(sites, output.skipped, new, data.rules or None)
        problems.extend(_api_problems(output.files, files))
        problems.extend(_plan_problems(output.plan, output.files, sites))
        if problems:
            raise OutputCheckError(_report(problems))

        tests: list[str] = ctx.metadata["tests"]
        if tests and output.files:
            run = await self._run_tests(data, new, set(output.files), tests)
            ctx.metadata["tests_after"] = run
            if not run.passed:
                before = ctx.metadata.get("tests_before")
                was = "passed" if before is not None and before.passed else "did not pass"
                raise OutputCheckError(
                    f"The tests fail on the migrated code (they {was} before). Behaviour must "
                    f"stay the same; fix the migration.\n\npytest output:\n{run.output}"
                )

    def postprocess(
        self, output: MigrationOutput, data: MigrationInput, ctx: RunContext
    ) -> MigrationOutput:
        files: dict[str, str] = ctx.metadata["files"]
        sites: dict[str, Site] = ctx.metadata["sites"]
        new = {**files, **output.files}
        diff = "".join(
            line
            for path in sorted(output.files)
            for line in difflib.unified_diff(
                files[path].splitlines(keepends=True),
                output.files[path].splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
        after = ctx.metadata.get("tests_after")
        return output.model_copy(
            update={
                "changed": sorted(output.files),
                "diff": diff,
                "sites_before": [_info(s) for s in sites.values()],
                "sites_after": [_info(s) for s in py_migrate.scan(new, data.rules or None)],
                "compiled": True,
                "tests_before": ctx.metadata.get("tests_before"),
                "tests_after": after,
                "behaviour_verified": bool(after and after.passed),
            }
        )

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _collect(data: MigrationInput) -> dict[str, str]:
        if not data.repo_path:
            return {p: c for p, c in data.files.items() if p.endswith(".py")}
        workspace = Workspace(data.repo_path)
        roots = [workspace.resolve(p) for p in data.paths] or [workspace.root]
        found: dict[str, str] = {}
        for root in roots:
            candidates = [root] if root.is_file() else sorted(root.rglob("*.py"))
            for path in candidates:
                if len(found) >= MAX_FILES:
                    break
                if path.is_symlink() or not path.is_file() or path.suffix != ".py":
                    continue
                if workspace.is_ignored(path) or workspace.is_denied(path):
                    continue
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                try:
                    found[workspace.relative(path)] = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        return found

    @staticmethod
    def _test_paths(data: MigrationInput, files: dict[str, str]) -> list[str]:
        if data.repo_path and data.tests:
            workspace = Workspace(data.repo_path)
            for rel in data.tests:
                if not workspace.resolve(rel).is_file():
                    raise MigrationError(f"test file '{rel}' not found in {data.repo_path}")
            return list(data.tests)
        return sorted(p for p in files if is_test_file(p))[:MAX_TEST_FILES]

    @staticmethod
    async def _run_tests(
        data: MigrationInput, sources: dict[str, str], changed: set[str], tests: list[str]
    ) -> pytest_runner.PytestRun:
        if not data.repo_path:
            return await pytest_runner.run_pytest(files=sources, tests=tests, measure=None)
        root = Workspace(data.repo_path).root
        files: dict[str, str] = {}
        for rel in tests:
            files[rel] = sources.get(rel) or (root / rel).read_text(encoding="utf-8")
            for parent in Path(rel).parents:
                for name in ("conftest.py", "__init__.py"):
                    support = (parent / name).as_posix()
                    if parent.name and (root / support).is_file():
                        files.setdefault(support, (root / support).read_text(encoding="utf-8"))
        for rel in sorted(changed - set(tests)):
            files.update(pytest_runner.shadow_files(root, rel, sources[rel]))
        return await pytest_runner.run_pytest(
            files=files, tests=tests, measure=None, extra_paths=[root, root / "src"]
        )


# ---------------------------------------------------------------------- checks


def _report(problems: list[str]) -> str:
    return (
        "The migration has problems:\n"
        + "\n".join(f"- {p}" for p in problems[: MAX_SHOWN * 2])
        + "\nFix them and return the complete JSON again."
    )


def _file_problems(
    returned: dict[str, str], originals: dict[str, str], everything_skipped: bool
) -> list[str]:
    problems: list[str] = []
    if not returned and not everything_skipped:
        problems.append("`files` is empty: return the complete new content of every changed file")
    for path, content in returned.items():
        if path not in originals:
            problems.append(f"{path} is not one of the files given; remove it")
            continue
        try:
            ast.parse(content, filename=path)
        except SyntaxError as exc:
            problems.append(f"{path} is not valid Python: line {exc.lineno}: {exc.msg}")
            continue
        if content.strip() == originals[path].strip():
            problems.append(f"{path} is unchanged; leave files you did not change out")
    return problems


def _skip_problems(skipped: list[Skip], sites: dict[str, Site]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for skip in skipped:
        if skip.site_id not in sites:
            problems.append(f"skipped site {skip.site_id} does not exist")
        elif skip.site_id in seen:
            problems.append(f"site {skip.site_id} is skipped twice")
        seen.add(skip.site_id)
    return problems


def _leftover_problems(
    sites: dict[str, Site],
    skipped: list[Skip],
    new: dict[str, str],
    rules: list[str] | None,
) -> list[str]:
    """Sites still in the new code beyond those deliberately skipped (counted per file and rule)."""
    allowed: dict[tuple[str, str], int] = {}
    for skip in skipped:
        if skip.site_id in sites:
            s = sites[skip.site_id]
            allowed[(s.file, s.rule)] = allowed.get((s.file, s.rule), 0) + 1
    remaining: dict[tuple[str, str], list[Site]] = {}
    for s in py_migrate.scan(new, rules):
        remaining.setdefault((s.file, s.rule), []).append(s)
    problems: list[str] = []
    for key, left in remaining.items():
        extra = left[allowed.get(key, 0) :]
        for s in extra[:MAX_SHOWN]:
            problems.append(
                f"{s.file}:{s.line} still has [{s.rule}] `{s.code}` ({s.detail}). Migrate it, "
                "or list its site id in `skipped` with a reason"
            )
    return problems


def _api_problems(returned: dict[str, str], originals: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for path, content in returned.items():
        changes = api_changes(
            public_api(parse(originals[path], path)), public_api(parse(content, path))
        )
        if changes:
            problems.append(
                f"{path}: the public API changed ({'; '.join(changes[:MAX_SHOWN])}); keep every "
                "public name and signature"
            )
    return problems


def _plan_problems(plan: list[Step], returned: dict[str, str], sites: dict[str, Site]) -> list[str]:
    problems: list[str] = []
    if not plan and returned:
        problems.append("`plan` is empty: add ordered steps")
    mentioned = {f for step in plan for f in step.files}
    problems.extend(
        f"the plan does not mention changed file {path}"
        for path in sorted(returned)
        if path not in mentioned
    )
    for step in plan:
        unknown = [sid for sid in step.sites if sid not in sites]
        if unknown:
            problems.append(f"step '{step.title}' refers to unknown site(s): {', '.join(unknown)}")
    return problems


def _info(site: Site) -> SiteInfo:
    return SiteInfo(
        rule=site.rule, file=site.file, line=site.line, code=site.code, detail=site.detail
    )


def _clip(text: str) -> str:
    return text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n# ...[truncated]"
