"""Golden eval cases for the Documentation agent, and how documentation is scored.

Each case in ``evals/*.json`` has an input (an undocumented or partly documented
module and a docstring style) and ``must_mention``: words that a correct
docstring for a given definition has to contain (e.g. the condition under
which it raises). Scoring re-checks everything itself:

1. the docstrings can be applied, and the code is unchanged,
2. every public definition has a docstring,
3. the new docstrings pass the rubric (``docstrings.lint``),
4. ``>>>`` examples pass as doctests (only when there are any),
5. the usage guide names every public top-level function and class, and its
   Python blocks parse,
6. one check per ``must_mention`` entry.

Deterministic, no LLM judge.
"""

from __future__ import annotations

import ast
import json
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.doc_gen.docstrings import (
    DocstringError,
    apply_docstrings,
    lint,
    placeable,
    python_blocks,
    source_lines,
)
from agentforge.agents.doc_gen.schema import DocGenInput, DocGenOutput
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check
from agentforge.tools.pytest_runner import run_pytest
from agentforge.tools.python_ast import definitions, has_docstring, parse, same_code


class Expectations(Contract):
    must_mention: dict[str, list[str]] = Field(
        default_factory=dict, description="qualname -> words its docstring must contain"
    )


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def doc_input(self) -> DocGenInput:
        return DocGenInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.doc_gen").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def _readme_checks(source: str, readme: str) -> list[Check]:
    top_level = [
        d.qualname
        for d in definitions(parse(source))
        if d.public and d.kind in ("class", "function")
    ]
    missing = [name for name in top_level if name not in readme]
    broken = 0
    for block in python_blocks(readme):
        try:
            ast.parse(block)
        except SyntaxError:
            broken += 1
    return [
        Check(
            name="readme covers the API",
            passed=bool(readme.strip()) and not missing,
            detail=f"not mentioned: {', '.join(missing)}" if missing else "",
        ),
        Check(name="readme code parses", passed=broken == 0, detail=f"{broken} broken block(s)"),
    ]


async def score(case: EvalCase, output: DocGenOutput) -> CaseScore:
    data = case.doc_input()
    source = data.files[data.target]
    try:
        applied = apply_docstrings(
            source,
            {d.name: d.docstring for d in output.docstrings},
            rewrite_existing=data.rewrite_existing,
        )
    except DocstringError as exc:
        return CaseScore(
            case=case.name, checks=[Check(name="docstrings apply", passed=False, detail=str(exc))]
        )
    documented = applied.source
    tree = parse(documented)
    by_name = {d.qualname: d for d in definitions(tree)}
    checks = [
        Check(name="docstrings apply", passed=True),
        Check(name="code unchanged", passed=same_code(source, documented)),
    ]

    lines = source_lines(documented)
    missing = [
        n
        for n, d in by_name.items()
        if d.public and placeable(d, lines) and not has_docstring(d.node)
    ]
    checks.append(
        Check(name="all public items documented", passed=not missing, detail=", ".join(missing))
    )
    problems = lint(documented, list(applied.written), data.style)
    checks.append(Check(name="rubric", passed=not problems, detail="; ".join(problems[:3])))

    if any(">>>" in d.docstring for d in output.docstrings if d.name in applied.written):
        run = await run_pytest(
            files={**data.files, data.target: documented},
            tests=[data.target],
            measure=None,
            args=["--doctest-modules"],
        )
        checks.append(Check(name="examples run", passed=run.passed, detail=f"{run.tests} doctests"))

    if data.readme:
        checks += _readme_checks(source, output.readme or "")

    for name, words in case.expect.must_mention.items():
        doc = (by_name[name].docstring or "") if name in by_name else ""
        absent = [w for w in words if w.lower() not in doc.lower()]
        checks.append(
            Check(
                name=f"{name} mentions {', '.join(words)}",
                passed=not absent,
                detail=f"missing: {', '.join(absent)}" if absent else "",
            )
        )
    return CaseScore(case=case.name, checks=checks)
