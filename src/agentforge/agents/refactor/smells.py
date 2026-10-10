"""Code smells, measured from the syntax tree: the Refactoring agent's starting point.

The Refactoring agent (Day 15) asks the model to fix the smells found here,
then measures again to prove the code got simpler. Kept deterministic and
explainable: each smell says what was measured, the limit, and where.

Smells:

- ``long_function``: more lines than ``max_lines`` (docstring excluded),
- ``complex_function``: cyclomatic complexity above ``max_complexity``,
- ``too_many_parameters``: more than ``max_parameters`` (``self``/``cls`` excluded),
- ``deep_nesting``: blocks nested deeper than ``max_nesting``,
- ``duplicate_code``: the same run of ``min_duplicate_statements`` statements
  appears twice (exact copies, ignoring layout and comments).
"""

from __future__ import annotations

import ast
from enum import StrEnum

from pydantic import Field

from agentforge.core.schemas import Contract
from agentforge.tools.python_ast import (
    FunctionNode,
    cyclomatic_complexity,
    definitions,
    length,
    max_nesting,
    parameters,
    parse,
)


class SmellKind(StrEnum):
    LONG_FUNCTION = "long_function"
    COMPLEX_FUNCTION = "complex_function"
    TOO_MANY_PARAMETERS = "too_many_parameters"
    DEEP_NESTING = "deep_nesting"
    DUPLICATE_CODE = "duplicate_code"


class Thresholds(Contract):
    max_lines: int = Field(default=50, ge=1)
    max_complexity: int = Field(default=10, ge=1)
    max_parameters: int = Field(default=5, ge=0)
    max_nesting: int = Field(default=3, ge=1)
    min_duplicate_statements: int = Field(default=4, ge=2)


class FunctionMetrics(Contract):
    name: str
    line: int
    lines: int
    complexity: int
    parameters: int
    nesting: int


class Smell(Contract):
    kind: SmellKind
    name: str = Field(description="Qualified name of the function, e.g. 'Cart.total'")
    line: int
    value: int
    limit: int
    message: str


def function_metrics(source: str) -> list[FunctionMetrics]:
    """Size and complexity numbers for every function and method, in source order."""
    result = []
    for d in definitions(parse(source)):
        if isinstance(d.node, ast.FunctionDef | ast.AsyncFunctionDef):
            result.append(
                FunctionMetrics(
                    name=d.qualname,
                    line=d.line,
                    lines=length(d.node),
                    complexity=cyclomatic_complexity(d.node),
                    parameters=len(parameters(d.node, is_method=d.kind == "method")),
                    nesting=max_nesting(d.node),
                )
            )
    return result


def _over_limit(m: FunctionMetrics, t: Thresholds) -> list[Smell]:
    rules = [
        (SmellKind.LONG_FUNCTION, m.lines, t.max_lines, "lines long"),
        (SmellKind.COMPLEX_FUNCTION, m.complexity, t.max_complexity, "cyclomatic complexity"),
        (SmellKind.TOO_MANY_PARAMETERS, m.parameters, t.max_parameters, "parameters"),
        (SmellKind.DEEP_NESTING, m.nesting, t.max_nesting, "levels of nesting"),
    ]
    return [
        Smell(
            kind=kind,
            name=m.name,
            line=m.line,
            value=value,
            limit=limit,
            message=f"{m.name}: {value} {what} (limit {limit})",
        )
        for kind, value, limit, what in rules
        if value > limit
    ]


def _blocks(node: ast.AST) -> list[list[ast.stmt]]:
    """Every statement list inside a function body (bodies of if/for/try/...), nested."""
    found: list[list[ast.stmt]] = []
    for child in ast.walk(node):
        if child is not node and isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for field in ("body", "orelse", "finalbody"):
            value = getattr(child, field, None)
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                found.append(value)
        for handler in getattr(child, "handlers", []) or []:
            found.append(handler.body)
    return found


def _duplicates(functions: list[tuple[str, FunctionNode]], size: int) -> list[Smell]:
    """Runs of at least ``size`` statements that also appear earlier; one smell per copy.

    A match is extended as far as both copies keep agreeing, so an 8-statement
    copy is one smell, not five overlapping ones.
    """
    blocks = [(name, block) for name, func in functions for block in _blocks(func)]
    dumps = [[ast.dump(s, include_attributes=False) for s in block] for _, block in blocks]
    seen: dict[tuple[str, ...], tuple[int, int]] = {}  # window -> (block number, index)
    smells: list[Smell] = []
    for b, (name, block) in enumerate(blocks):
        start = 0
        while start + size <= len(block):
            key = tuple(dumps[b][start : start + size])
            origin = seen.get(key)
            if origin is None or (origin[0] == b and origin[1] + size > start):
                seen.setdefault(key, (b, start))
                start += 1
                continue
            ob, oi = origin
            n = size
            while (
                start + n < len(block)
                and oi + n < len(dumps[ob])
                and (ob != b or oi + n < start)
                and dumps[b][start + n] == dumps[ob][oi + n]
            ):
                n += 1
            other_name, other = blocks[ob]
            first, last = block[start].lineno, _end(block[start + n - 1])
            smells.append(
                Smell(
                    kind=SmellKind.DUPLICATE_CODE,
                    name=name,
                    line=first,
                    value=n,
                    limit=size - 1,
                    message=(
                        f"{name}: lines {first}-{last} repeat lines "
                        f"{other[oi].lineno}-{_end(other[oi + n - 1])} of {other_name}"
                    ),
                )
            )
            start += n
    return smells


def _end(node: ast.stmt) -> int:
    return node.end_lineno or node.lineno


def find_smells(source: str, thresholds: Thresholds | None = None) -> list[Smell]:
    """All smells in ``source``, ordered by line."""
    limits = thresholds or Thresholds()
    smells: list[Smell] = []
    for metrics in function_metrics(source):
        smells += _over_limit(metrics, limits)
    functions = [
        (d.qualname, d.node)
        for d in definitions(parse(source))
        if isinstance(d.node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    smells += _duplicates(functions, limits.min_duplicate_statements)
    return sorted(smells, key=lambda s: (s.line, s.kind.value))
