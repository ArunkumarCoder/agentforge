"""Getting validated, typed output out of free-form model text.

Models are asked to answer with a single JSON object matching the agent's
output schema. In practice they sometimes wrap it in a ```json fence, add a
sentence before it, or get a field wrong. ``parse_model_output`` handles the
wrapping; validation errors come back as short, readable feedback that is sent
to the model once so it can correct itself (see ``LLMAgent``).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from agentforge.core.errors import AgentForgeError

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n\s*```", re.DOTALL)


class OutputParseError(AgentForgeError):
    """The model's reply could not be turned into the output model."""

    def __init__(self, message: str, *, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


class OutputCheckError(OutputParseError):
    """The output is well-formed but wrong (e.g. a patch that does not apply)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, raw="")


def extract_json(text: str) -> Any:
    """Find and decode the JSON object in a model reply.

    Tries, in order: the whole text, the first ```json fenced block, and the
    span from the first ``{`` to the last ``}``.
    """
    candidates = [text.strip()]
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    last_error = "no JSON object found"
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = f"{exc.msg} at line {exc.lineno} column {exc.colno}"
    raise OutputParseError(f"Reply is not valid JSON ({last_error}).", raw=text)


def parse_model_output[T: BaseModel](text: str, model: type[T]) -> T:
    """Extract JSON from ``text`` and validate it against ``model``."""
    data = extract_json(text)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise OutputParseError(
            f"JSON does not match the required schema: {describe_errors(exc)}", raw=text
        ) from exc


def describe_errors(exc: ValidationError, limit: int = 8) -> str:
    """Compact, model-friendly list of validation problems."""
    parts = []
    for error in exc.errors()[:limit]:
        where = ".".join(str(p) for p in error["loc"]) or "(root)"
        parts.append(f"{where}: {error['msg']}")
    more = exc.error_count() - limit
    return "; ".join(parts) + (f"; and {more} more" if more > 0 else "")


def schema_for_prompt(model: type[BaseModel]) -> str:
    """The output model's JSON Schema, formatted for inclusion in a prompt."""
    return json.dumps(model.model_json_schema(), indent=2)
