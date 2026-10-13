"""Which agents have golden eval cases, and how to run and score them.

A :class:`Suite` ties together four things every agent's ``scoring.py``
already has: how to load its cases, how to turn a case into the agent's input,
how to score the agent's output, and the agent's registry name.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from agentforge.evals.scoring import CaseScore

type ScoreFn = Callable[[Any, Any], CaseScore | Awaitable[CaseScore]]


@dataclass(frozen=True)
class Suite:
    """Golden cases for one agent."""

    agent: str
    load_cases: Callable[[], Sequence[Any]]
    make_input: Callable[[Any], BaseModel]
    score: ScoreFn

    async def score_case(self, case: Any, output: Any) -> CaseScore:
        result = self.score(case, output)
        return await result if inspect.isawaitable(result) else result


def all_suites() -> dict[str, Suite]:
    """Every shipped suite by agent name. Imports are lazy to keep startup and cycles light."""
    from agentforge.agents.api_design import scoring as api_design
    from agentforge.agents.bug_fix import scoring as bug_fix
    from agentforge.agents.code_review import scoring as code_review
    from agentforge.agents.db_schema import scoring as db_schema
    from agentforge.agents.doc_gen import scoring as doc_gen
    from agentforge.agents.refactor import scoring as refactor
    from agentforge.agents.test_gen import scoring as test_gen

    suites = [
        Suite("api-design", api_design.load_cases, lambda c: c.design_input(), api_design.score),
        Suite("code-review", code_review.load_cases, lambda c: c.review_input(), code_review.score),
        Suite("db-schema", db_schema.load_cases, lambda c: c.schema_input(), db_schema.score),
        Suite("bug-fix", bug_fix.load_cases, lambda c: c.fix_input(), bug_fix.score),
        Suite("test-gen", test_gen.load_cases, lambda c: c.gen_input(), test_gen.score),
        Suite("doc-gen", doc_gen.load_cases, lambda c: c.doc_input(), doc_gen.score),
        Suite("refactor", refactor.load_cases, lambda c: c.refactor_input(), refactor.score),
    ]
    return {s.agent: s for s in sorted(suites, key=lambda s: s.agent)}
