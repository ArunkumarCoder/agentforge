"""API Design agent: spec validation and linting with repair, canonical output, golden cases.

FakeProvider plays the model with scripted JSON answers. The golden-case tests also prove that
each case's scoring fails when the spec is wrong, so the evals measure what they claim to.
"""

import copy
import json
from typing import Any

import pytest

from agentforge.agents.api_design import ApiDesignAgent, ApiDesignInput, ApiDesignOutput
from agentforge.agents.api_design.scoring import load_cases, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools import openapi
from tests.agents.apidesign_refs import REFERENCE_SPECS

CASES = {c.name: c for c in load_cases()}
TASKS = CASES["tasks"]


def answer(spec: str | None = None, **overrides: Any) -> str:
    base = {
        "summary": "A task API with CRUD, completion and filtering.",
        "spec": spec if spec is not None else REFERENCE_SPECS["tasks"],
        "assumptions": ["ids are UUIDs"],
    }
    return json.dumps({**base, **overrides})


def edited(name: str, edit: Any) -> str:
    spec = openapi.load_spec(REFERENCE_SPECS[name])
    edit(spec)
    return openapi.dump_spec(spec)


async def run(llm: FakeProvider, **data: Any) -> ApiDesignOutput:
    result = await ApiDesignAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("api-design") is ApiDesignAgent
    assert "api" in ApiDesignAgent.card().tags


def test_input_defaults_and_rules() -> None:
    data = ApiDesignInput(requirements="A thing that does things")
    assert (data.auth.value, data.pagination.value, data.api_version) == ("none", "none", "1.0.0")
    with pytest.raises(ValueError):
        ApiDesignInput(requirements="short")


async def test_designs_validates_and_fills_the_report() -> None:
    llm = FakeProvider(script=[answer()])
    output = await run(llm, **TASKS.input)
    assert output.valid is True
    assert output.operations[:2] == ["GET /tasks", "POST /tasks"]
    assert len(output.operations) == 6
    assert output.schemas == ["Error", "NewTask", "Task", "TaskPatch"]
    assert output.warnings == []
    assert output.assumptions == ["ids are UUIDs"]
    assert openapi.load_spec(output.spec)["openapi"] == "3.1.0"
    system, user = (m.content for m in llm.requests[0].messages[:2])
    assert '"spec"' in system  # output schema is in the prompt
    assert "OpenAPI 3.1.0" in system
    assert "every endpoint needs a bearer token" in user
    assert "`limit` and `offset`" in user
    assert "- Title: Task API" in user
    assert "to-do list service" in user


async def test_messages_mention_instructions_and_defaults() -> None:
    llm = FakeProvider(script=[answer(spec=_plain())])
    await run(
        llm, requirements="A tiny status service with one endpoint.", instructions="Keep it small"
    )
    user = llm.requests[0].messages[1].content
    assert "Authentication: none (define no security scheme)" in user
    assert "Pagination of lists: none" in user
    assert "Keep it small" in user


def _plain() -> str:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "Status", "version": "1.0.0"},
        "paths": {
            "/status": {
                "get": {
                    "operationId": "getStatus",
                    "summary": "Service status",
                    "tags": ["status"],
                    "responses": {
                        "200": {"description": "ok"},
                        "default": {"description": "error"},
                    },
                }
            }
        },
    }
    return openapi.dump_spec(spec)


async def test_output_spec_is_canonical_yaml_even_if_the_model_sent_json() -> None:
    as_json = json.dumps(openapi.load_spec(REFERENCE_SPECS["tasks"]))
    output = await run(FakeProvider(script=[answer(as_json)]), **TASKS.input)
    assert output.spec == REFERENCE_SPECS["tasks"]


async def test_invalid_spec_gets_one_repair_with_the_location() -> None:
    bad = edited("tasks", lambda s: s["info"].pop("version"))
    llm = FakeProvider(script=[answer(bad), answer()])
    await run(llm, **TASKS.input)
    feedback = llm.requests[1].messages[-1].content
    assert "not valid OpenAPI 3.1" in feedback
    assert "$.info" in feedback
    assert "version" in feedback


async def test_design_rule_errors_are_listed_for_the_repair() -> None:
    def edit(s: dict[str, Any]) -> None:
        del s["paths"]["/tasks"]["get"]["operationId"]
        del s["paths"]["/tasks"]["get"]["parameters"]

    llm = FakeProvider(script=[answer(edited("tasks", edit)), answer()])
    await run(llm, **TASKS.input)
    feedback = llm.requests[1].messages[-1].content
    assert "breaks design rules" in feedback
    assert "GET /tasks has no operationId" in feedback
    assert "pagination" in feedback


async def test_not_yaml_gets_repaired() -> None:
    llm = FakeProvider(script=[answer("this is: [not closed"), answer()])
    await run(llm, **TASKS.input)
    assert "The spec is unusable" in llm.requests[1].messages[-1].content


async def test_wrong_version_string_is_rejected() -> None:
    wrong = edited("tasks", lambda s: s["info"].update(version="9.9"))
    llm = FakeProvider(script=[answer(wrong), answer()])
    await run(llm, **TASKS.input)
    assert "info.version must be '1.0.0'" in llm.requests[1].messages[-1].content


async def test_requested_version_is_honoured() -> None:
    v2 = edited("tasks", lambda s: s["info"].update(version="2.3.0"))
    output = await run(FakeProvider(script=[answer(v2)]), **{**TASKS.input, "api_version": "2.3.0"})
    assert openapi.load_spec(output.spec)["info"]["version"] == "2.3.0"


async def test_many_problems_are_capped_in_the_message() -> None:
    def edit(s: dict[str, Any]) -> None:
        for i in range(20):
            s["paths"][f"/x{i}"] = {"get": {"responses": {"200": {"description": "x"}}}}

    llm = FakeProvider(script=[answer(edited("tasks", edit)), answer()])
    await run(llm, **TASKS.input)
    feedback = llm.requests[1].messages[-1].content
    assert "...and " in feedback and "more" in feedback


async def test_a_spec_that_stays_broken_is_an_output_error() -> None:
    bad = answer(edited("tasks", lambda s: s["info"].pop("version")))
    result = await ApiDesignAgent(FakeProvider(script=[bad, bad, bad])).arun(TASKS.input)
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "$.info" in result.error.message


async def test_warnings_are_reported_not_blocking() -> None:
    spec = edited("tasks", lambda s: s["paths"]["/tasks"]["post"].pop("description"))
    output = await run(FakeProvider(script=[answer(spec)]), **TASKS.input)
    assert output.warnings == ["POST /tasks has no description"]


# --------------------------------------------------------------------------- golden cases


def test_five_cases_with_unique_names() -> None:
    assert sorted(CASES) == ["auth_users", "bookstore", "documents", "orders", "tasks"]


@pytest.mark.parametrize("name", sorted(CASES))
async def test_reference_spec_scores_full_marks(name: str) -> None:
    case = CASES[name]
    llm = FakeProvider(script=[answer(REFERENCE_SPECS[name])])
    output = await run(llm, **case.input)
    card = score(case, output)
    assert card.passed, [c for c in card.checks if not c.passed]
    assert card.score == 1.0


def _scored(name: str, edit: Any) -> dict[str, bool]:
    case = CASES[name]
    spec = openapi.load_spec(REFERENCE_SPECS[name])
    edit(spec)
    out = ApiDesignOutput(summary="edited reference", spec=openapi.dump_spec(spec))
    return {c.name: c.passed for c in score(case, out).checks}


def test_scoring_notices_invalid_and_unparseable_specs() -> None:
    out = ApiDesignOutput(summary="broken output", spec="- just\n- a list\n- of things")
    assert [c.name for c in score(TASKS, out).checks] == ["spec parses"]
    checks = _scored("tasks", lambda s: s["info"].pop("version"))
    assert checks == {"valid OpenAPI 3.1": False}


def test_scoring_notices_missing_operations_and_scope_creep() -> None:
    assert not _scored("tasks", lambda s: s["paths"].pop("/tasks/{id}/complete"))[
        "has every operation"
    ]

    def bloat(s: dict[str, Any]) -> None:
        template = s["paths"]["/tasks"]["get"]
        for i in range(5):
            node = copy.deepcopy(template)
            node["operationId"] = f"extra{i}"
            s["paths"][f"/extras{i}"] = {"get": node}

    assert not _scored("tasks", bloat)["stays in scope"]


def test_scoring_notices_missing_fields_and_wrong_statuses() -> None:
    def drop_field(s: dict[str, Any]) -> None:
        del s["components"]["schemas"]["NewTask"]["properties"]["due_date"]

    assert not _scored("tasks", drop_field)["declares the required fields"]

    def wrong_status(s: dict[str, Any]) -> None:
        post = s["paths"]["/tasks"]["post"]["responses"]
        post["200"] = post.pop("201")

    assert not _scored("tasks", wrong_status)["uses the right status codes"]


def test_scoring_notices_protected_login_and_wrong_media_and_missing_enum() -> None:
    def protect(s: dict[str, Any]) -> None:
        del s["paths"]["/auth/login"]["post"]["security"]

    assert not _scored("auth_users", protect)["public endpoints are open"]

    def enum(s: dict[str, Any]) -> None:
        s["components"]["schemas"]["User"]["properties"]["role"]["enum"] = ["member"]

    assert not _scored("auth_users", enum)["declares the enums"]

    def media(s: dict[str, Any]) -> None:
        body = s["paths"]["/documents"]["post"]["requestBody"]["content"]
        body["application/json"] = body.pop("multipart/form-data")

    assert not _scored("documents", media)["uses the right media types"]


def test_scoring_reports_missing_operations_in_fields_check() -> None:
    checks = _scored("tasks", lambda s: s["paths"].pop("/tasks"))
    assert not checks["declares the required fields"]
    assert not checks["uses the right status codes"]
