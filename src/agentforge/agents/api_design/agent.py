"""Agent 6 — API Design: requirements in, a validated OpenAPI 3.1 document out."""

from __future__ import annotations

from agentforge.agents.api_design.schema import ApiDesignInput, ApiDesignOutput, Auth, Pagination
from agentforge.core.agent import RunContext
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools import openapi

MAX_REPORTED = 12


@register_agent
class ApiDesignAgent(LLMAgent[ApiDesignInput, ApiDesignOutput]):
    name = "api-design"
    description = (
        "Turns requirements into an OpenAPI 3.1 spec that validates against the official "
        "schema and follows design rules (operationIds, error responses, auth, pagination)."
    )
    version = "0.1.0"
    tags = ("architecture", "api")
    input_model = ApiDesignInput
    output_model = ApiDesignOutput
    max_repairs = 2
    max_tokens = 8192
    timeout_s = 300.0

    def build_user_message(self, data: ApiDesignInput, ctx: RunContext) -> str:
        parts = [
            "Design this API.",
            f"\n## Requirements\n\n{data.requirements.strip()}",
            "\n## Conventions\n",
            f"- API version: {data.api_version}",
            f"- Authentication: {_AUTH_TEXT[data.auth]}",
            f"- Pagination of lists: {_PAGINATION_TEXT[data.pagination]}",
        ]
        if data.title:
            parts.append(f"- Title: {data.title}")
        if data.instructions:
            parts.append(f"\n## Extra instructions\n\n{data.instructions.strip()}")
        return "\n".join(parts)

    async def check(self, output: ApiDesignOutput, data: ApiDesignInput, ctx: RunContext) -> None:
        """The spec must parse, validate as OpenAPI 3.1 and pass the lint errors."""
        try:
            spec = openapi.load_spec(output.spec)
        except openapi.SpecError as exc:
            raise OutputCheckError(f"The spec is unusable: {exc}") from exc
        problems = openapi.validate(spec)
        if problems:
            raise OutputCheckError(_report("The spec is not valid OpenAPI 3.1", problems))
        found = openapi.lint(spec, auth=data.auth.value, pagination=data.pagination.value)
        if found.errors:
            raise OutputCheckError(_report("The spec breaks design rules", found.errors))
        if spec["info"].get("version") != data.api_version:
            raise OutputCheckError(f"info.version must be '{data.api_version}'.")
        ctx.metadata["spec"] = spec
        ctx.metadata["warnings"] = found.warnings

    def postprocess(
        self, output: ApiDesignOutput, data: ApiDesignInput, ctx: RunContext
    ) -> ApiDesignOutput:
        spec = ctx.metadata["spec"]
        return output.model_copy(
            update={
                "spec": openapi.dump_spec(spec),
                "valid": True,
                "operations": [op.key for op in openapi.operations(spec)],
                "schemas": openapi.schema_names(spec),
                "warnings": ctx.metadata["warnings"],
            }
        )


_AUTH_TEXT = {
    Auth.NONE: "none (define no security scheme)",
    Auth.BEARER: "every endpoint needs a bearer token (public endpoints such as login excepted)",
    Auth.API_KEY: "every endpoint needs an API key in a header",
}
_PAGINATION_TEXT = {
    Pagination.NONE: "none",
    Pagination.OFFSET: "offset: query parameters `limit` and `offset` on every list endpoint",
    Pagination.CURSOR: "cursor: query parameters `limit` and `cursor` on every list endpoint",
}


def _report(title: str, problems: list[str]) -> str:
    shown = problems[:MAX_REPORTED]
    more = len(problems) - len(shown)
    lines = "\n".join(f"- {p}" for p in shown)
    return f"{title}. Fix these and return the complete corrected spec:\n{lines}" + (
        f"\n- ...and {more} more" if more > 0 else ""
    )
