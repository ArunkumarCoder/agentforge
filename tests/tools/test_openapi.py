"""OpenAPI helpers: parsing, official validation, lint rules and spec inspection."""

import copy
from typing import Any

import pytest

from agentforge.tools import openapi
from tests.agents.apidesign_refs import REFERENCE_SPECS

TASKS = openapi.load_spec(REFERENCE_SPECS["tasks"])
ORDERS = openapi.load_spec(REFERENCE_SPECS["orders"])


def mutated(spec: dict[str, Any], edit: Any) -> dict[str, Any]:
    clone = copy.deepcopy(spec)
    edit(clone)
    return clone


def errors(spec: dict[str, Any], **conventions: str) -> list[str]:
    return openapi.lint(spec, **conventions).errors


# --------------------------------------------------------------------------- loading


def test_load_accepts_yaml_and_json_and_dump_round_trips() -> None:
    assert openapi.load_spec('{"a": 1}') == {"a": 1}
    assert openapi.load_spec("a: 1\nb: [x]\n") == {"a": 1, "b": ["x"]}
    assert openapi.load_spec(openapi.dump_spec(TASKS)) == TASKS


@pytest.mark.parametrize("text", ["- a\n- b", "just words", "a: [unclosed"])
def test_load_rejects_non_mappings_and_broken_yaml(text: str) -> None:
    with pytest.raises(openapi.SpecError):
        openapi.load_spec(text)


def test_path_and_key_normalisation() -> None:
    assert openapi.normalize_path("/a/{userId}/b/{id}") == "/a/{}/b/{}"
    assert openapi.normalize_key("get /tasks/{taskId}") == "GET /tasks/{}"


# --------------------------------------------------------------------------- validation


def test_reference_specs_are_valid() -> None:
    for text in REFERENCE_SPECS.values():
        assert openapi.validate(openapi.load_spec(text)) == []


def test_validate_requires_openapi_3_1() -> None:
    old = mutated(TASKS, lambda s: s.update(openapi="3.0.3"))
    assert "3.1.x" in openapi.validate(old)[0]
    assert "3.1.x" in openapi.validate({})[0]


def test_validate_reports_where_the_problem_is() -> None:
    broken = mutated(TASKS, lambda s: s["info"].pop("version"))
    problems = openapi.validate(broken)
    assert problems
    assert "$.info" in problems[0]
    assert "version" in problems[0]


def test_validate_catches_dangling_refs() -> None:
    def edit(s: dict[str, Any]) -> None:
        s["paths"]["/tasks"]["post"]["responses"]["201"] = {"$ref": "#/components/responses/Nope"}

    problems = openapi.validate(mutated(TASKS, edit))
    assert any("does not exist" in p and "/components/responses/Nope" in p for p in problems)


def test_validate_caps_the_number_of_problems() -> None:
    def edit(s: dict[str, Any]) -> None:
        for i in range(30):
            s["paths"][f"/p{i}"] = {"get": {}}

    assert len(openapi.validate(mutated(TASKS, edit))) <= 8


# --------------------------------------------------------------------------- lint


def test_reference_specs_lint_clean() -> None:
    conventions = {
        "tasks": ("bearer", "offset"),
        "bookstore": ("none", "cursor"),
        "auth_users": ("bearer", "none"),
        "documents": ("api_key", "none"),
        "orders": ("bearer", "cursor"),
    }
    for name, (auth, pagination) in conventions.items():
        found = openapi.lint(
            openapi.load_spec(REFERENCE_SPECS[name]), auth=auth, pagination=pagination
        )
        assert (found.errors, found.warnings) == ([], []), name


def test_missing_operation_id_summary_and_duplicates() -> None:
    def edit(s: dict[str, Any]) -> None:
        del s["paths"]["/tasks"]["get"]["operationId"]
        del s["paths"]["/tasks"]["post"]["summary"]
        s["paths"]["/tasks/{id}"]["get"]["operationId"] = "createTask"

    found = errors(mutated(TASKS, edit), auth="bearer", pagination="offset")
    assert "GET /tasks has no operationId" in found
    assert "POST /tasks has no summary" in found
    assert any("reuses operationId 'createTask'" in e for e in found)


def test_path_parameters_must_be_declared_and_required() -> None:
    def undeclared(s: dict[str, Any]) -> None:
        del s["paths"]["/tasks/{id}"]["get"]["parameters"]

    def optional(s: dict[str, Any]) -> None:
        s["paths"]["/tasks/{id}"]["get"]["parameters"][0]["required"] = False

    assert any(
        "declares no path parameter 'id'" in e
        for e in errors(mutated(TASKS, undeclared), auth="bearer")
    )
    assert any("must be required" in e for e in errors(mutated(TASKS, optional), auth="bearer"))


def test_path_level_parameters_count() -> None:
    def edit(s: dict[str, Any]) -> None:
        item = s["paths"]["/tasks/{id}"]
        item["parameters"] = [
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
        ]
        for method in ("get", "patch", "delete"):
            item[method].pop("parameters", None)

    found = errors(mutated(TASKS, edit), auth="bearer")
    assert not any("path parameter" in e for e in found)


def test_responses_need_success_and_error() -> None:
    def edit(s: dict[str, Any]) -> None:
        s["paths"]["/tasks/{id}"]["get"]["responses"] = {"200": {"description": "ok"}}
        s["paths"]["/tasks/{id}"]["delete"]["responses"] = {"default": {"description": "err"}}

    found = errors(mutated(TASKS, edit), auth="bearer")
    assert "GET /tasks/{id} documents no error response (4xx or default)" in found
    assert "DELETE /tasks/{id} has no 2xx response" in found


def test_bodies_where_they_do_not_belong() -> None:
    def edit(s: dict[str, Any]) -> None:
        s["paths"]["/tasks/{id}"]["get"]["requestBody"] = {"content": {}}
        s["paths"]["/tasks/{id}"]["delete"]["responses"]["204"] = {
            "description": "gone",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }

    found = errors(mutated(TASKS, edit), auth="bearer")
    assert "GET /tasks/{id} must not have a requestBody" in found
    assert any("a 204 response cannot have a body" in e for e in found)


def test_no_operations_is_an_error() -> None:
    assert errors({"openapi": "3.1.0", "paths": {}}) == ["the spec defines no operations"]


def test_security_rules() -> None:
    no_scheme = mutated(TASKS, lambda s: s["components"].pop("securitySchemes"))
    assert any("bearer scheme" in e for e in errors(no_scheme, auth="bearer"))
    assert any("apiKey scheme" in e for e in errors(TASKS, auth="api_key"))
    unprotected = mutated(TASKS, lambda s: s.pop("security"))
    assert sum("not covered by any security" in e for e in errors(unprotected, auth="bearer")) == 6
    assert any(
        "no authentication was asked" in w for w in openapi.lint(TASKS, auth="none").warnings
    )


def test_explicit_empty_security_makes_an_endpoint_public() -> None:
    def edit(s: dict[str, Any]) -> None:
        s.pop("security")
        for item in s["paths"].values():
            for node in item.values():
                node["security"] = (
                    [] if node["operationId"] == "listTasks" else [{"bearerAuth": []}]
                )

    assert errors(mutated(TASKS, edit), auth="bearer", pagination="offset") == []


def test_list_endpoints_need_the_asked_for_pagination() -> None:
    unpaged = mutated(TASKS, lambda s: s["paths"]["/tasks"]["get"].pop("parameters"))
    found = errors(unpaged, auth="bearer", pagination="offset")
    assert any("GET /tasks returns a list" in e and "limit, offset" in e for e in found)
    assert errors(unpaged, auth="bearer", pagination="none") == []
    wrong_style = errors(TASKS, auth="bearer", pagination="cursor")
    assert any("cursor" in e for e in wrong_style)


def test_single_resources_and_actions_are_not_lists() -> None:
    assert not openapi.is_collection(ORDERS, _op(ORDERS, "GET /orders/{orderId}"))
    assert openapi.is_collection(ORDERS, _op(ORDERS, "GET /orders"))
    assert openapi.is_collection(ORDERS, _op(ORDERS, "GET /orders/{orderId}/items"))
    assert not openapi.is_collection(ORDERS, _op(ORDERS, "POST /orders"))


def test_style_findings_are_warnings_only() -> None:
    def edit(s: dict[str, Any]) -> None:
        s["paths"]["/getTasks"] = s["paths"]["/tasks"]
        s["paths"]["/task_items"] = {
            "get": {
                "operationId": "x",
                "summary": "x",
                "responses": {"200": {"description": "x"}, "default": {"description": "x"}},
            }
        }
        s["components"]["schemas"]["Unused"] = {"type": "object"}
        del s["paths"]["/tasks"]["post"]["description"]

    found = openapi.lint(mutated(TASKS, edit), auth="bearer")
    text = "\n".join(found.warnings)
    assert "looks like a verb" in text
    assert "kebab-case" in text
    assert "components.schemas.Unused is never referenced" in text
    assert "POST /tasks has no description" in text
    assert "GET /task_items has no tags" in text


def test_create_without_201_and_delete_status_warn() -> None:
    def edit(s: dict[str, Any]) -> None:
        post = s["paths"]["/tasks"]["post"]["responses"]
        post["200"] = post.pop("201")
        delete = s["paths"]["/tasks/{id}"]["delete"]["responses"]
        delete["404x"] = delete.pop("204")  # leaves no 2xx at all
        s["paths"]["/tasks/{id}/complete"]["post"].pop("requestBody", None)

    warnings = openapi.lint(mutated(TASKS, edit), auth="bearer").warnings
    assert "POST /tasks creates something but does not return 201" in warnings


# --------------------------------------------------------------------------- inspection


def _op(spec: dict[str, Any], key: str) -> openapi.Operation:
    return next(op for op in openapi.operations(spec) if op.key == key)


def test_operations_in_document_order() -> None:
    keys = [op.key for op in openapi.operations(TASKS)]
    assert keys[:2] == ["GET /tasks", "POST /tasks"]
    assert len(keys) == 6
    assert openapi.operations({"paths": "nope"}) == []


def test_schema_names_and_security_kinds() -> None:
    assert openapi.schema_names(TASKS) == ["Error", "NewTask", "Task", "TaskPatch"]
    assert openapi.security_scheme_types(TASKS) == {"bearer"}
    documents = openapi.load_spec(REFERENCE_SPECS["documents"])
    assert openapi.security_scheme_types(documents) == {"apiKey"}
    assert openapi.security_scheme_types({}) == set()


def test_request_and_response_fields_follow_refs() -> None:
    post = _op(TASKS, "POST /tasks")
    assert openapi.request_fields(TASKS, post) == {"title", "due_date"}
    assert {"id", "title", "done"} <= openapi.response_fields(TASKS, post)
    assert openapi.request_fields(TASKS, _op(TASKS, "GET /tasks")) == set()
    listing = openapi.response_fields(TASKS, _op(TASKS, "GET /tasks"))
    assert {"items", "total", "id", "title"} <= listing  # a page includes its items' fields


def test_parameters_merge_levels_and_resolve_refs() -> None:
    names = [p["name"] for p in openapi.parameters(TASKS, _op(TASKS, "GET /tasks"))]
    assert names == ["limit", "offset", "done"]


def test_resolve_handles_bad_refs() -> None:
    assert openapi.resolve(TASKS, {"$ref": "#/components/schemas/Task"})["type"] == "object"
    assert openapi.resolve(TASKS, {"$ref": "#/components/schemas/Missing"}) == {}
    external = {"$ref": "other.yaml#/X"}
    assert openapi.resolve(TASKS, external) is external
    loop = {"components": {"schemas": {"A": {"$ref": "#/components/schemas/A"}}}}
    assert isinstance(openapi.resolve(loop, {"$ref": "#/components/schemas/A"}), dict)


def test_success_codes() -> None:
    assert openapi.success_codes(_op(TASKS, "POST /tasks")) == ["201"]
