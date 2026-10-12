"""Golden eval cases for the Refactoring agent, and how a refactoring is scored.

Each case in ``evals/*.json`` has an input (a smelly module, its tests and the
thresholds) and expectations:

- ``fixed``: kinds of smell that must be gone from the result,
- ``hidden_tests``: extra tests the model never sees. They stop a "refactoring"
  that only passes the visible tests.

Scoring re-checks everything itself instead of trusting the agent's report:

1. the result is valid Python,
2. the public API is unchanged,
3. the visible tests pass, then the hidden tests,
4. each smell kind in ``fixed`` is gone,
5. there are fewer smells than before and no new *kind* of smell.

Deterministic, no LLM judge.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.refactor.schema import RefactorInput, RefactorOutput
from agentforge.agents.refactor.smells import SmellKind, find_smells
from agentforge.agents.test_gen.schema import is_test_file
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check
from agentforge.tools.pytest_runner import run_pytest
from agentforge.tools.python_ast import PythonSourceError, api_changes, parse, public_api


class Expectations(Contract):
    fixed: list[SmellKind] = Field(min_length=1)
    hidden_tests: dict[str, str] = Field(default_factory=dict)


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def refactor_input(self) -> RefactorInput:
        return RefactorInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.refactor").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def _last_line(output: str) -> str:
    lines = output.strip().splitlines()
    return lines[-1] if lines else ""


async def score(case: EvalCase, output: RefactorOutput) -> CaseScore:
    data = case.refactor_input()
    original = data.files[data.target]
    new = output.refactored_source
    try:
        tree = parse(new, data.target)
    except PythonSourceError as exc:
        return CaseScore(
            case=case.name, checks=[Check(name="valid Python", passed=False, detail=str(exc))]
        )
    checks = [Check(name="valid Python", passed=True)]

    changes = api_changes(public_api(parse(original)), public_api(tree))
    checks.append(Check(name="public API kept", passed=not changes, detail="; ".join(changes[:3])))

    files = {**data.files, data.target: new}
    visible = [p for p in data.files if is_test_file(p)]
    run = await run_pytest(files=files, tests=visible, measure=None)
    checks.append(Check(name="tests pass", passed=run.passed, detail=_last_line(run.output)))
    if case.expect.hidden_tests:
        hidden = await run_pytest(
            files={**files, **case.expect.hidden_tests},
            tests=list(case.expect.hidden_tests),
            measure=None,
        )
        checks.append(
            Check(name="hidden tests pass", passed=hidden.passed, detail=_last_line(hidden.output))
        )

    before = find_smells(original, data.thresholds)
    after = find_smells(new, data.thresholds)
    for kind in case.expect.fixed:
        left = [s.message for s in after if s.kind is kind]
        checks.append(
            Check(name=f"fixes {kind.value}", passed=not left, detail="; ".join(left[:2]))
        )
    new_kinds = {s.kind for s in after} - {s.kind for s in before}
    checks.append(
        Check(
            name="fewer smells, none of a new kind",
            passed=len(after) < len(before) and not new_kinds,
            detail=f"{len(before)} -> {len(after)}"
            + (f"; new: {', '.join(sorted(k.value for k in new_kinds))}" if new_kinds else ""),
        )
    )
    return CaseScore(case=case.name, checks=checks)
