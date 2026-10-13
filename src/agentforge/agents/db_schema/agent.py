"""Agent 7 — Database Schema: entities in, checked PostgreSQL DDL, ORM models and an ERD out.

The model writes DDL (and SQLAlchemy models). AgentForge then checks them against reality:

1. the DDL is parsed by PostgreSQL's own parser, and only schema definitions are allowed,
2. design rules: primary keys, foreign keys that point at real, unique, same-typed
   columns, parents created first, known types,
3. the models are imported in a separate interpreter and must match the DDL table by table,
4. if a PostgreSQL server is configured, the DDL is run on it and rolled back.

Each failure goes back to the model (up to two corrections). The ER diagram is generated
from the checked DDL, not written by the model, so it cannot disagree with it.
"""

from __future__ import annotations

from agentforge.agents.db_schema.schema import DbSchemaInput, DbSchemaOutput
from agentforge.core.agent import RunContext
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.registry import register_agent
from agentforge.core.structured import OutputCheckError
from agentforge.tools import postgres, sa_models, sql_schema

MAX_REPORTED = 12


@register_agent
class DbSchemaAgent(LLMAgent[DbSchemaInput, DbSchemaOutput]):
    name = "db-schema"
    description = (
        "Turns entity descriptions into PostgreSQL DDL (parsed with Postgres's own parser, "
        "linted, optionally executed on a real server), SQLAlchemy models that match it, "
        "and a Mermaid ER diagram."
    )
    version = "0.1.0"
    tags = ("architecture", "database")
    input_model = DbSchemaInput
    output_model = DbSchemaOutput
    max_repairs = 2
    max_tokens = 8192
    timeout_s = 300.0

    def build_user_message(self, data: DbSchemaInput, ctx: RunContext) -> str:
        parts = [
            "Design this database.",
            f"\n## Requirements\n\n{data.requirements.strip()}",
            "\n## Conventions\n",
            f"- Primary keys: {_ID_TEXT[data.id_style.value]}",
            "- SQLAlchemy models: "
            + ("write them" if data.orm_models else "not wanted; leave `models` empty"),
        ]
        if data.instructions:
            parts.append(f"\n## Extra instructions\n\n{data.instructions.strip()}")
        return "\n".join(parts)

    async def check(self, output: DbSchemaOutput, data: DbSchemaInput, ctx: RunContext) -> None:
        try:
            schema = sql_schema.parse_ddl(output.ddl)
        except sql_schema.DdlError as exc:
            raise OutputCheckError(f"The DDL is not valid PostgreSQL: {exc}") from exc
        found = sql_schema.lint(schema)
        if found.errors:
            raise OutputCheckError(_report("The DDL breaks schema rules", found.errors))

        if data.orm_models:
            await self._check_models(output, schema)

        executed: bool | None = None
        if postgres.available():
            run = await postgres.execute_ddl(output.ddl)
            if not run.ok:
                raise OutputCheckError(f"PostgreSQL rejected the DDL: {run.error}")
            executed = True
        ctx.metadata.update(schema=schema, warnings=found.warnings, executed=executed)

    @staticmethod
    async def _check_models(output: DbSchemaOutput, schema: sql_schema.Schema) -> None:
        if not output.models.strip():
            raise OutputCheckError("SQLAlchemy models were asked for, but `models` is empty.")
        try:
            models = await sa_models.introspect(output.models)
        except sa_models.ModelsError as exc:
            raise OutputCheckError(f"The models do not load: {exc}") from exc
        problems = sa_models.differences(schema, models)
        if problems:
            raise OutputCheckError(_report("The models do not match the DDL", problems))

    def postprocess(
        self, output: DbSchemaOutput, data: DbSchemaInput, ctx: RunContext
    ) -> DbSchemaOutput:
        schema: sql_schema.Schema = ctx.metadata["schema"]
        return output.model_copy(
            update={
                "erd": sql_schema.to_mermaid(schema),
                "tables": list(schema.order),
                "valid": True,
                "executed": ctx.metadata["executed"],
                "warnings": ctx.metadata["warnings"],
                "models": output.models if data.orm_models else "",
            }
        )


_ID_TEXT = {
    "uuid": "surrogate `uuid` keys with `DEFAULT gen_random_uuid()`",
    "bigint": "surrogate `bigint GENERATED ALWAYS AS IDENTITY` keys",
}


def _report(title: str, problems: list[str]) -> str:
    shown = problems[:MAX_REPORTED]
    more = len(problems) - len(shown)
    lines = "\n".join(f"- {p}" for p in shown)
    return f"{title}. Fix these and return the complete corrected output:\n{lines}" + (
        f"\n- ...and {more} more" if more > 0 else ""
    )
