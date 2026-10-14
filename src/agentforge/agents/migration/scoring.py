"""Golden eval cases for the Migration agent, and how a migration is scored.

Each case in ``evals/*.json`` is a small project using old Python APIs, with tests. The
answer key says:

- ``hidden_tests``: extra tests the model never sees. They stop a "migration" that only
  satisfies the visible tests (for example turning naive datetimes into aware ones),
- ``unchanged``: files that need nothing and must come back untouched.

Scoring re-checks everything itself instead of trusting the agent's report:

1. every returned file is valid Python,
2. each rule that had sites is fully gone, and no new site appeared,
3. the public API of every changed file is kept,
4. the visible tests pass, then the hidden tests,
5. test files keep every assertion (a "migration" must not weaken a test),
6. files listed in ``unchanged`` are not changed,
7. the plan mentions every changed file.

Deterministic, no LLM judge.
"""

from __future__ import annotations

import ast
import json
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.migration.schema import MigrationInput, MigrationOutput
from agentforge.agents.test_gen.schema import is_test_file
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check
from agentforge.tools.py_migrate import scan
from agentforge.tools.pytest_runner import run_pytest
from agentforge.tools.python_ast import PythonSourceError, api_changes, parse, public_api


class Expectations(Contract):
    hidden_tests: dict[str, str] = Field(default_factory=dict)
    unchanged: list[str] = Field(default_factory=list)


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def migration_input(self) -> MigrationInput:
        return MigrationInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.migration").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def assertion_count(source: str) -> int:
    """How many assertions a test file makes: ``assert``, ``self.assert*``, ``pytest.raises``."""
    count = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assert) or (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and (node.func.attr.startswith("assert") or node.func.attr in {"raises", "fail"})
        ):
            count += 1
    return count


def _last_line(output: str) -> str:
    lines = output.strip().splitlines()
    return lines[-1] if lines else ""


async def score(case: EvalCase, output: MigrationOutput) -> CaseScore:
    data = case.migration_input()
    originals = {p: c for p, c in data.files.items() if p.endswith(".py")}
    new = {**originals, **output.files}
    rules = data.rules or None

    for path, content in output.files.items():
        try:
            parse(content, path)
        except PythonSourceError as exc:
            return CaseScore(
                case=case.name, checks=[Check(name="valid Python", passed=False, detail=str(exc))]
            )
    checks = [Check(name="valid Python", passed=True)]

    before = scan(originals, rules)
    after = scan(new, rules)
    for rule in sorted({s.rule for s in before}):
        left = [f"{s.file}:{s.line}" for s in after if s.rule == rule]
        checks.append(Check(name=f"migrates {rule}", passed=not left, detail=", ".join(left[:3])))
    new_rules = sorted({s.rule for s in after} - {s.rule for s in before})
    checks.append(
        Check(name="no new kinds of site", passed=not new_rules, detail=", ".join(new_rules))
    )

    changes = [
        f"{path}: {c}"
        for path in output.files
        if path in originals
        for c in api_changes(public_api(parse(originals[path])), public_api(parse(new[path])))
    ]
    checks.append(Check(name="public API kept", passed=not changes, detail="; ".join(changes[:3])))

    visible = [p for p in new if is_test_file(p)]
    run = await run_pytest(files=new, tests=visible, measure=None)
    checks.append(Check(name="tests pass", passed=run.passed, detail=_last_line(run.output)))
    if case.expect.hidden_tests:
        hidden = await run_pytest(
            files={**new, **case.expect.hidden_tests},
            tests=list(case.expect.hidden_tests),
            measure=None,
        )
        checks.append(
            Check(name="hidden tests pass", passed=hidden.passed, detail=_last_line(hidden.output))
        )

    weakened = [
        p
        for p in visible
        if p in originals and assertion_count(new[p]) < assertion_count(originals[p])
    ]
    checks.append(
        Check(name="tests keep their assertions", passed=not weakened, detail=", ".join(weakened))
    )
    touched = [p for p in case.expect.unchanged if new.get(p) != originals.get(p)]
    checks.append(
        Check(name="untouched files stay untouched", passed=not touched, detail=", ".join(touched))
    )
    unplanned = sorted(set(output.files) - {f for step in output.plan for f in step.files})
    checks.append(
        Check(
            name="plan covers every changed file", passed=not unplanned, detail=", ".join(unplanned)
        )
    )
    return CaseScore(case=case.name, checks=checks)
