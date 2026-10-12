"""Golden eval cases for the API Design agent, and how a spec is scored against one.

Each case in ``evals/*.json`` has an input (requirements and conventions) and
expectations about the spec:

- ``operations``: ``"METHOD /path"`` keys that must exist (path parameter names
  do not matter: ``/tasks/{id}`` equals ``/tasks/{taskId}``),
- ``fields``: per operation, property names the request body or the response
  must declare,
- ``statuses``: per operation, a success code it must document,
- ``public``: operations that must opt out of authentication,
- ``media``: per operation, a media type its request or a success response uses,
- ``enums``: value lists, each of which some ``enum`` in the spec must contain.

Scoring is deterministic: the spec is validated with the official OpenAPI 3.1
schema, linted with the same rules the agent had to pass, then checked against
the expectations. No LLM judge.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.api_design.schema import ApiDesignInput, ApiDesignOutput
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check
from agentforge.tools import openapi

EXTRA_OPERATIONS_ALLOWED = 3


class FieldExpectation(Contract):
    request: list[str] = Field(default_factory=list)
    response: list[str] = Field(default_factory=list)


class Expectations(Contract):
    operations: list[str] = Field(min_length=1)
    fields: dict[str, FieldExpectation] = Field(default_factory=dict)
    statuses: dict[str, str] = Field(default_factory=dict)
    public: list[str] = Field(default_factory=list)
    media: dict[str, str] = Field(default_factory=dict)
    enums: list[list[str]] = Field(default_factory=list)


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def design_input(self) -> ApiDesignInput:
        return ApiDesignInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.api_design").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


def score(case: EvalCase, output: ApiDesignOutput) -> CaseScore:
    want = case.expect
    data = case.design_input()
    checks: list[Check] = []

    try:
        spec = openapi.load_spec(output.spec)
    except openapi.SpecError as exc:
        return CaseScore(
            case=case.name, checks=[Check(name="spec parses", passed=False, detail=str(exc))]
        )

    problems = openapi.validate(spec)
    checks.append(
        Check(name="valid OpenAPI 3.1", passed=not problems, detail="; ".join(problems[:3]))
    )
    if problems:
        return CaseScore(case=case.name, checks=checks)

    found = openapi.lint(spec, auth=data.auth.value, pagination=data.pagination.value)
    checks.append(
        Check(name="design rules", passed=not found.errors, detail="; ".join(found.errors[:3]))
    )

    ops = {openapi.normalize_key(op.key): op for op in openapi.operations(spec)}
    expected = [openapi.normalize_key(k) for k in want.operations]
    missing = [k for k in expected if k not in ops]
    checks.append(
        Check(
            name="has every operation", passed=not missing, detail="missing " + ", ".join(missing)
        )
    )
    extra = len(ops) - len(expected)
    checks.append(
        Check(
            name="stays in scope",
            passed=extra <= EXTRA_OPERATIONS_ALLOWED,
            detail=f"{extra} operations beyond the {len(expected)} asked for",
        )
    )

    checks.append(_fields(spec, ops, want))
    if want.statuses:
        checks.append(_statuses(ops, want))
    if want.public:
        checks.append(_public(ops, want))
    if want.media:
        checks.append(_media(spec, ops, want))
    if want.enums:
        checks.append(_enums(spec, want))
    return CaseScore(case=case.name, checks=checks)


def _fields(spec: openapi.Spec, ops: dict[str, openapi.Operation], want: Expectations) -> Check:
    gaps: list[str] = []
    for key, expect in want.fields.items():
        op = ops.get(openapi.normalize_key(key))
        if op is None:
            gaps.append(f"{key}: no such operation")
            continue
        for side, names, have in (
            ("request", expect.request, openapi.request_fields(spec, op)),
            ("response", expect.response, openapi.response_fields(spec, op)),
        ):
            lacking = [n for n in names if n not in have]
            if lacking:
                gaps.append(f"{key} {side} lacks {', '.join(lacking)}")
    return Check(name="declares the required fields", passed=not gaps, detail="; ".join(gaps))


def _statuses(ops: dict[str, openapi.Operation], want: Expectations) -> Check:
    gaps = []
    for key, code in want.statuses.items():
        op = ops.get(openapi.normalize_key(key))
        if op is None or code not in openapi.success_codes(op):
            gaps.append(f"{key} should return {code}")
    return Check(name="uses the right status codes", passed=not gaps, detail="; ".join(gaps))


def _public(ops: dict[str, openapi.Operation], want: Expectations) -> Check:
    gaps = []
    for key in want.public:
        op = ops.get(openapi.normalize_key(key))
        if op is None or op.node.get("security") != []:
            gaps.append(f"{key} should set security: []")
    return Check(name="public endpoints are open", passed=not gaps, detail="; ".join(gaps))


def _media(spec: openapi.Spec, ops: dict[str, openapi.Operation], want: Expectations) -> Check:
    gaps = []
    for key, media in want.media.items():
        op = ops.get(openapi.normalize_key(key))
        if op is None or media not in _media_types(spec, op):
            gaps.append(f"{key} should use {media}")
    return Check(name="uses the right media types", passed=not gaps, detail="; ".join(gaps))


def _media_types(spec: openapi.Spec, op: openapi.Operation) -> set[str]:
    holders = [op.node.get("requestBody")]
    responses = op.node.get("responses")
    for code, response in (responses if isinstance(responses, dict) else {}).items():
        if str(code).startswith("2"):
            holders.append(response)
    found: set[str] = set()
    for holder in holders:
        resolved = openapi.resolve(spec, holder)
        content = resolved.get("content") if isinstance(resolved, dict) else None
        found |= set(content) if isinstance(content, dict) else set()
    return found


def _enums(spec: openapi.Spec, want: Expectations) -> Check:
    available = [set(map(str, values)) for values in _walk_enums(spec)]
    gaps = [
        "/".join(values)
        for values in want.enums
        if not any(set(values) <= have for have in available)
    ]
    return Check(
        name="declares the enums", passed=not gaps, detail="no enum has " + "; ".join(gaps)
    )


def _walk_enums(node: Any) -> list[list[Any]]:
    found: list[list[Any]] = []
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list):
            found.append(node["enum"])
        for value in node.values():
            found += _walk_enums(value)
    elif isinstance(node, list):
        for value in node:
            found += _walk_enums(value)
    return found
