"""Golden eval cases for the Test Generation agent, and how generated tests are scored.

Each case in ``evals/*.json`` has an input (a module, sometimes with existing
tests) and expectations:

- ``min_coverage``: the new tests must run at least this percent of the target,
- ``mutants``: small, deliberate bugs (as patches). Good tests **fail** on each
  one. This is what separates real tests from tests that only call the code.

Scoring re-runs everything itself instead of trusting the agent's report:

1. the tests pass on the original module,
2. coverage reaches ``min_coverage``,
3. coverage is higher than the existing tests' (when the case has some),
4. one check per mutant: the tests fail when that bug is applied (mutants run
   in parallel).

Deterministic, no LLM judge.
"""

from __future__ import annotations

import asyncio
import json
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.test_gen.schema import GenerateTestsInput, GenerateTestsOutput, is_test_file
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check
from agentforge.tools.patching import apply_patch
from agentforge.tools.pytest_runner import run_pytest


class Mutant(Contract):
    name: str
    patch: str


class Expectations(Contract):
    min_coverage: float = Field(ge=0, le=100)
    mutants: list[Mutant] = Field(default_factory=list)


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def gen_input(self) -> GenerateTestsInput:
        return GenerateTestsInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.test_gen").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def _last_line(output: str) -> str:
    lines = output.strip().splitlines()
    return lines[-1] if lines else ""


async def score(case: EvalCase, output: GenerateTestsOutput) -> CaseScore:
    data = case.gen_input()
    files, target = dict(data.files), data.target
    tests = {output.test_path: output.test_code}
    checks: list[Check] = []

    run = await run_pytest(files={**files, **tests}, tests=[output.test_path], measure=target)
    checks.append(Check(name="tests pass", passed=run.passed, detail=_last_line(run.output)))

    coverage = run.coverage or 0.0
    want = case.expect.min_coverage
    checks.append(
        Check(
            name=f"coverage >= {want:g}%",
            passed=run.passed and coverage >= want,
            detail=f"got {coverage:g}%",
        )
    )

    existing = sorted(p for p in files if is_test_file(p))
    if existing:
        before = (await run_pytest(files=files, tests=existing, measure=target)).coverage or 0.0
        checks.append(
            Check(
                name="coverage goes up",
                passed=run.passed and coverage > before,
                detail=f"{before:g}% -> {coverage:g}%",
            )
        )

    mutant_runs = await asyncio.gather(
        *(
            run_pytest(
                files={**files, **apply_patch(files, m.patch), **tests},
                tests=[output.test_path],
                measure=None,
            )
            for m in case.expect.mutants
        )
    )
    for mutant, result in zip(case.expect.mutants, mutant_runs, strict=True):
        caught = not result.passed
        checks.append(
            Check(
                name=f"catches: {mutant.name}",
                passed=caught,
                detail="" if caught else "the tests still pass with this bug",
            )
        )
    return CaseScore(case=case.name, checks=checks)
