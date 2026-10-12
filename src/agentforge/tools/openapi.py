"""OpenAPI 3.1 helpers: parse, validate against the official schema, lint, and inspect a spec.

``validate`` answers "is this a legal OpenAPI 3.1 document?" using
``openapi-spec-validator`` (which also checks that every ``$ref`` resolves).
``lint`` answers the next question, "is it a *good* API?", with a small set of
deterministic rules. Each finding is either an **error** (the agent must fix it)
or a **warning** (reported, never blocking).

Everything works on plain dicts, so specs from the model, from YAML files and
from tests are treated alike. Nothing here touches the network or the disk.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import yaml
from openapi_spec_validator import OpenAPIV31SpecValidator
from referencing.exceptions import Unresolvable

METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
BODYLESS = ("get", "delete", "head", "trace")
_PATH_PARAM = re.compile(r"\{([^}]+)\}")
_VERB_PREFIX = re.compile(r"^(get|create|update|delete|add|remove|list|fetch|set)[A-Z_-]", re.I)
_MAX_ERRORS = 8

type Spec = dict[str, Any]


class SpecError(ValueError):
    """The text is not a YAML or JSON mapping at all."""


@dataclass(frozen=True)
class Operation:
    """One HTTP operation of the spec."""

    method: str  # upper case
    path: str
    node: dict[str, Any]
    path_item: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"

    @property
    def operation_id(self) -> str | None:
        value = self.node.get("operationId")
        return value if isinstance(value, str) else None


@dataclass
class Lint:
    """Lint findings, split by whether the agent must act on them."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- loading


def load_spec(text: str) -> Spec:
    """Parse YAML or JSON (JSON is YAML) into a dict, or raise :class:`SpecError`."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"not valid YAML or JSON: {str(exc).splitlines()[0]}") from exc
    if not isinstance(data, dict):
        raise SpecError("the document must be a mapping with 'openapi', 'info' and 'paths'")
    return data


class _PlainDumper(yaml.SafeDumper):
    """Writes a shared object out each time it appears instead of as an ``&anchor``/``*alias``."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_spec(spec: Spec) -> str:
    """Canonical YAML: keys in the order given, no anchors, unicode kept."""
    return yaml.dump(spec, Dumper=_PlainDumper, sort_keys=False, allow_unicode=True, width=100)


def normalize_path(path: str) -> str:
    """``/users/{userId}/books`` and ``/users/{id}/books`` are the same route: ``{}``."""
    return _PATH_PARAM.sub("{}", path)


def normalize_key(key: str) -> str:
    """Normalise an ``"GET /users/{id}"`` style key (method case, parameter names)."""
    method, _, path = key.strip().partition(" ")
    return f"{method.upper()} {normalize_path(path.strip())}"


# --------------------------------------------------------------------------- validation


def validate(spec: Spec) -> list[str]:
    """Problems that make this an invalid OpenAPI 3.1 document (empty when valid)."""
    version = spec.get("openapi")
    if not isinstance(version, str) or not version.startswith("3.1"):
        return [f"'openapi' must be a 3.1.x version string, got {version!r}"]
    problems: list[str] = []
    try:
        for error in OpenAPIV31SpecValidator(spec).iter_errors():
            where = getattr(error, "json_path", "$")
            problems.append(f"{where}: {error.message.splitlines()[0][:200]}")
            if len(problems) >= _MAX_ERRORS:
                break
    except Unresolvable as exc:  # a $ref that points at nothing stops the validator itself
        problems.append(f"$ref does not resolve: {str(exc).split(' within ')[0]}")
    return problems


# --------------------------------------------------------------------------- inspection


def resolve(spec: Spec, node: Any, depth: int = 8) -> Any:
    """Follow local ``$ref`` links (``#/components/...``); other nodes are returned as is."""
    while depth and isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref: str = node["$ref"]
        if not ref.startswith("#/"):
            return node
        target: Any = spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return {}
            target = target[part]
        node = target
        depth -= 1
    return node


def operations(spec: Spec) -> list[Operation]:
    """Every operation, in document order."""
    found = []
    paths = spec.get("paths")
    for path, item in (paths if isinstance(paths, dict) else {}).items():
        if not isinstance(item, dict):
            continue
        for method in METHODS:
            node = item.get(method)
            if isinstance(node, dict):
                found.append(Operation(method.upper(), str(path), node, item))
    return found


def schema_names(spec: Spec) -> list[str]:
    components = spec.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    return sorted(schemas) if isinstance(schemas, dict) else []


def parameters(spec: Spec, op: Operation) -> list[dict[str, Any]]:
    """Path-level plus operation-level parameters, refs resolved (operation level wins)."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in [*(op.path_item.get("parameters") or []), *(op.node.get("parameters") or [])]:
        param = resolve(spec, raw)
        if isinstance(param, dict) and "name" in param:
            merged[(str(param.get("in")), str(param["name"]))] = param
    return list(merged.values())


def success_codes(op: Operation) -> list[str]:
    return [c for c in _responses(op) if c.startswith("2")]


def _responses(op: Operation) -> dict[str, Any]:
    responses = op.node.get("responses")
    return {str(k): v for k, v in responses.items()} if isinstance(responses, dict) else {}


def json_schema(spec: Spec, content_holder: Any) -> dict[str, Any]:
    """The JSON (or first) media-type schema of a request body or response, refs resolved."""
    holder = resolve(spec, content_holder)
    content = holder.get("content") if isinstance(holder, dict) else None
    if not isinstance(content, dict) or not content:
        return {}
    media = content.get("application/json") or next(iter(content.values()))
    schema = resolve(spec, media.get("schema", {})) if isinstance(media, dict) else {}
    return schema if isinstance(schema, dict) else {}


def property_names(spec: Spec, schema: Any, depth: int = 4) -> set[str]:
    """Property names a schema declares, following refs, ``allOf`` and array ``items``."""
    schema = resolve(spec, schema)
    if not isinstance(schema, dict) or depth == 0:
        return set()
    names = set(schema["properties"]) if isinstance(schema.get("properties"), dict) else set()
    for key in ("allOf", "oneOf", "anyOf"):
        for part in schema.get(key) or []:
            names |= property_names(spec, part, depth - 1)
    if "items" in schema:
        names |= property_names(spec, schema["items"], depth - 1)
    return names


def request_fields(spec: Spec, op: Operation) -> set[str]:
    return property_names(spec, json_schema(spec, op.node.get("requestBody")))


def response_fields(spec: Spec, op: Operation) -> set[str]:
    """Properties of the first success response; for a wrapped list this includes the item's."""
    for code in sorted(success_codes(op)):
        schema = json_schema(spec, _responses(op)[code])
        if schema:
            names = property_names(spec, schema)
            for value in (schema.get("properties") or {}).values():
                names |= property_names(spec, value)
            return names
    return set()


def is_collection(spec: Spec, op: Operation) -> bool:
    """A GET that returns a list: an array, or an object holding an array property."""
    if op.method != "GET" or op.path.rstrip("/").endswith("}"):
        return False  # a single resource, even if it holds a list inside
    for code in success_codes(op):
        schema = json_schema(spec, _responses(op)[code])
        if schema.get("type") == "array":
            return True
        for name, value in (schema.get("properties") or {}).items():
            value = resolve(spec, value)
            if name in LIST_KEYS and isinstance(value, dict) and value.get("type") == "array":
                return True
    return False


def security_scheme_types(spec: Spec) -> set[str]:
    """What kinds of auth the spec declares: ``bearer``, ``apiKey``, ``oauth2``, ``basic`` ..."""
    components = spec.get("components")
    schemes = components.get("securitySchemes") if isinstance(components, dict) else None
    kinds: set[str] = set()
    for scheme in (schemes or {}).values():
        scheme = resolve(spec, scheme)
        if not isinstance(scheme, dict):
            continue
        if scheme.get("type") == "http":
            kinds.add(str(scheme.get("scheme", "http")).lower())
        else:
            kinds.add(str(scheme.get("type")))
    return kinds


# --------------------------------------------------------------------------- linting

LIST_KEYS = {"items", "data", "results", "records", "entries"}
PAGINATION_PARAMS = {"offset": {"limit", "offset"}, "cursor": {"limit", "cursor"}}


def lint(spec: Spec, *, auth: str = "none", pagination: str = "none") -> Lint:
    """Check API design rules.

    ``auth`` is none, bearer or api_key; ``pagination`` is none, offset or cursor.
    """
    result = Lint()
    ops = operations(spec)
    if not ops:
        result.errors.append("the spec defines no operations")
    _lint_security(spec, ops, auth, result)
    seen: dict[str, str] = {}
    for op in ops:
        _lint_operation(spec, op, seen, result)
        if pagination in PAGINATION_PARAMS and is_collection(spec, op):
            have = {str(p["name"]) for p in parameters(spec, op)}
            missing = sorted(PAGINATION_PARAMS[pagination] - have)
            if missing:
                result.errors.append(
                    f"{op.key} returns a list but has no {pagination} pagination "
                    f"parameter(s): {', '.join(missing)}"
                )
    _lint_paths(spec, result)
    used = set(iter_schema_refs(spec.get("paths", {})))
    for name in schema_names(spec):
        reachable = any(r.startswith(f"#/components/schemas/{name}") for r in used)
        if not reachable and not _used_by_components(spec, name):
            result.warnings.append(f"components.schemas.{name} is never referenced")
    return result


def _used_by_components(spec: Spec, name: str) -> bool:
    target = f"#/components/schemas/{name}"
    components = spec.get("components", {})
    for section, entries in components.items():
        for key, value in entries.items() if isinstance(entries, dict) else []:
            if section == "schemas" and key == name:
                continue
            if any(r == target for r in iter_schema_refs(value)):
                return True
    return False


def _lint_security(spec: Spec, ops: list[Operation], auth: str, result: Lint) -> None:
    kinds = security_scheme_types(spec)
    wanted = {"bearer": {"bearer"}, "api_key": {"apiKey"}}.get(auth)
    if wanted and not kinds & wanted:
        label = "an HTTP bearer scheme" if auth == "bearer" else "an apiKey scheme"
        result.errors.append(f"components.securitySchemes must define {label}")
    if auth == "none" and kinds:
        result.warnings.append("security schemes are defined but no authentication was asked for")
    if not kinds:
        return
    global_security = spec.get("security")
    for op in ops:
        if "security" in op.node:
            continue  # explicit, including `security: []` for public endpoints
        if not global_security:
            result.errors.append(
                f"{op.key} is not covered by any security requirement "
                "(add top-level 'security', or 'security: []' on public endpoints)"
            )


def _lint_operation(spec: Spec, op: Operation, seen: dict[str, str], result: Lint) -> None:
    key = op.key
    op_id = op.operation_id
    if not op_id:
        result.errors.append(f"{key} has no operationId")
    elif op_id in seen:
        result.errors.append(f"{key} reuses operationId '{op_id}' of {seen[op_id]}")
    else:
        seen[op_id] = key
    if not op.node.get("summary"):
        result.errors.append(f"{key} has no summary")
    if not op.node.get("tags"):
        result.warnings.append(f"{key} has no tags")

    declared = {p["name"] for p in parameters(spec, op) if p.get("in") == "path"}
    for name in _PATH_PARAM.findall(op.path):
        if name not in declared:
            result.errors.append(f"{key} uses {{{name}}} but declares no path parameter '{name}'")
    for param in parameters(spec, op):
        if param.get("in") == "path" and param.get("required") is not True:
            result.errors.append(f"{key}: path parameter '{param['name']}' must be required: true")

    if op.method.lower() in BODYLESS and "requestBody" in op.node:
        result.errors.append(f"{key} must not have a requestBody")
    action = _is_action(op.path)
    if op.method in ("POST", "PUT", "PATCH") and "requestBody" not in op.node and not action:
        result.warnings.append(f"{key} has no requestBody")

    responses = _responses(op)
    codes = success_codes(op)
    if not codes:
        result.errors.append(f"{key} has no 2xx response")
    if not any(c.startswith(("4", "5")) or c == "default" for c in responses):
        result.errors.append(f"{key} documents no error response (4xx or default)")
    creates = op.method == "POST" and codes and not action
    if creates and not set(codes) & {"201", "202"} and not _is_rpc(op.path):
        result.warnings.append(f"{key} creates something but does not return 201")
    if op.method == "DELETE" and codes and not set(codes) & {"200", "202", "204"}:
        result.warnings.append(f"{key} should return 204 (or 200/202)")
    for code in codes:
        if code == "204" and json_schema(spec, responses[code]):
            result.errors.append(f"{key}: a 204 response cannot have a body")
    if not op.node.get("description"):
        result.warnings.append(f"{key} has no description")


def _is_rpc(path: str) -> bool:
    return any(word in path for word in ("search", "login", "token"))


def _is_action(path: str) -> bool:
    """``/orders/{id}/cancel``: a verb under an id, not a collection to create into."""
    parts = path.strip("/").split("/")
    return len(parts) >= 2 and parts[-2].startswith("{") and not parts[-1].startswith("{")


def _lint_paths(spec: Spec, result: Lint) -> None:
    paths = spec.get("paths")
    for path in paths if isinstance(paths, dict) else []:
        for segment in str(path).strip("/").split("/"):
            if segment.startswith("{") or not segment:
                continue
            if segment != segment.lower() or "_" in segment:
                result.warnings.append(f"{path}: use lower-case kebab-case segments ('{segment}')")
            if _VERB_PREFIX.match(segment):
                result.warnings.append(
                    f"{path}: '{segment}' looks like a verb; paths should be nouns"
                )


def iter_schema_refs(node: Any) -> Iterator[str]:
    """Every ``$ref`` string below ``node`` (used to spot unused components)."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from iter_schema_refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_schema_refs(value)
