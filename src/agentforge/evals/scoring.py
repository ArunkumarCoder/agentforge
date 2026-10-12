"""The result of scoring one eval case, shared by every agent's ``scoring.py``.

A score is a list of named checks (each passed or failed, with a short detail).
A case passes only when every check passes; ``score`` is the fraction that did.
"""

from __future__ import annotations

from agentforge.core.schemas import Contract


class Check(Contract):
    name: str
    passed: bool
    detail: str = ""


class CaseScore(Contract):
    case: str
    checks: list[Check]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def score(self) -> float:
        return sum(c.passed for c in self.checks) / len(self.checks) if self.checks else 0.0
