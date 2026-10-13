"""Golden eval cases for the Database Schema agent, and how a schema is scored against one.

Each case in ``evals/*.json`` has an input (requirements) and expectations about the DDL:

- ``tables``: tables that must exist,
- ``columns``: per table, columns that must exist,
- ``foreign_keys``: ``"orders.customer_id -> customers.id"`` links that must exist,
- ``unique``: ``"order_items(order_id,product_id)"`` column sets that must be unique,
- ``checks``: tables that must carry at least one ``CHECK`` constraint,
- ``types``: ``"products.price": "numeric"`` base types that columns must have,
- ``required`` / ``nullable``: ``"table.column"`` entries that must be NOT NULL / nullable,
- ``enums``: value lists that some ``CREATE TYPE ... AS ENUM`` must contain.

Scoring is deterministic: the DDL is parsed with PostgreSQL's parser and linted with the
same rules the agent had to pass, the models are loaded and compared with it, and the
expectations are checked on the parsed schema. When ``AGENTFORGE_CHECK_DATABASE_URL`` is
set, the DDL is also run on that server. No LLM judge.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

from pydantic import Field

from agentforge.agents.db_schema.schema import DbSchemaInput, DbSchemaOutput
from agentforge.core.schemas import Contract
from agentforge.evals.scoring import CaseScore, Check
from agentforge.tools import postgres, sa_models, sql_schema

EXTRA_TABLES_ALLOWED = 2


class Expectations(Contract):
    tables: list[str] = Field(min_length=1)
    columns: dict[str, list[str]] = Field(default_factory=dict)
    foreign_keys: list[str] = Field(default_factory=list)
    unique: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    types: dict[str, str] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    nullable: list[str] = Field(default_factory=list)
    enums: list[list[str]] = Field(default_factory=list)


class EvalCase(Contract):
    name: str
    description: str
    input: dict[str, Any]
    expect: Expectations

    def schema_input(self) -> DbSchemaInput:
        return DbSchemaInput.model_validate(self.input)


def load_cases() -> list[EvalCase]:
    """All golden cases shipped with the agent, sorted by name."""
    folder = resources.files("agentforge.agents.db_schema").joinpath("evals")
    cases = [
        EvalCase.model_validate(json.loads(entry.read_text(encoding="utf-8")))
        for entry in folder.iterdir()
        if entry.name.endswith(".json")
    ]
    return sorted(cases, key=lambda c: c.name)


async def score(case: EvalCase, output: DbSchemaOutput) -> CaseScore:
    want = case.expect
    data = case.schema_input()
    checks: list[Check] = []

    try:
        schema = sql_schema.parse_ddl(output.ddl)
    except sql_schema.DdlError as exc:
        return CaseScore(
            case=case.name, checks=[Check(name="DDL parses", passed=False, detail=str(exc))]
        )
    checks.append(Check(name="DDL parses", passed=True))

    found = sql_schema.lint(schema)
    checks.append(
        Check(name="schema rules", passed=not found.errors, detail="; ".join(found.errors[:3]))
    )

    missing = [t for t in want.tables if t not in schema.tables]
    checks.append(
        Check(name="has every table", passed=not missing, detail="missing " + ", ".join(missing))
    )
    extra = len(schema.tables) - len(want.tables)
    checks.append(
        Check(
            name="stays in scope",
            passed=extra <= EXTRA_TABLES_ALLOWED,
            detail=f"{extra} tables beyond the {len(want.tables)} asked for",
        )
    )
    checks.append(_columns(schema, want))
    if want.foreign_keys:
        checks.append(_foreign_keys(schema, want))
    if want.unique:
        checks.append(_unique(schema, want))
    if want.checks:
        gaps = [t for t in want.checks if t not in schema.tables or not schema.tables[t].checks]
        checks.append(
            Check(
                name="has CHECK constraints", passed=not gaps, detail="none on " + ", ".join(gaps)
            )
        )
    if want.types:
        checks.append(_types(schema, want))
    if want.required or want.nullable:
        checks.append(_nullability(schema, want))
    if want.enums:
        gaps = [
            "/".join(values)
            for values in want.enums
            if not any(set(values) <= set(have) for have in schema.enums.values())
        ]
        checks.append(
            Check(
                name="declares the enums", passed=not gaps, detail="no enum has " + "; ".join(gaps)
            )
        )

    if data.orm_models:
        checks.append(await _models(schema, output))
    checks.append(_diagram(schema, output))
    if postgres.available():
        run = await postgres.execute_ddl(output.ddl)
        checks.append(Check(name="runs on PostgreSQL", passed=run.ok, detail=run.error))
    return CaseScore(case=case.name, checks=checks)


def _columns(schema: sql_schema.Schema, want: Expectations) -> Check:
    gaps = []
    for table, names in want.columns.items():
        found = schema.tables.get(table)
        lacking = [n for n in names if found is None or n not in found.columns]
        if lacking:
            gaps.append(f"{table} lacks {', '.join(lacking)}")
    return Check(name="has the required columns", passed=not gaps, detail="; ".join(gaps))


def _foreign_keys(schema: sql_schema.Schema, want: Expectations) -> Check:
    have = {
        f"{t.name}.{c} -> {fk.ref_table}.{r}"
        for t in schema.tables.values()
        for fk in t.foreign_keys
        for c, r in zip(fk.columns, fk.ref_columns, strict=False)
    }
    gaps = [k for k in want.foreign_keys if k.replace("  ", " ") not in have]
    return Check(name="has the foreign keys", passed=not gaps, detail="missing " + "; ".join(gaps))


def _unique(schema: sql_schema.Schema, want: Expectations) -> Check:
    gaps = []
    for entry in want.unique:
        name, _, rest = entry.partition("(")
        columns = tuple(c.strip() for c in rest.rstrip(")").split(","))
        table = schema.tables.get(name)
        sets = schema.unique_sets(table) if table else []
        if columns not in [tuple(s) for s in sets]:
            gaps.append(entry)
    return Check(
        name="enforces uniqueness", passed=not gaps, detail="not unique: " + "; ".join(gaps)
    )


def _types(schema: sql_schema.Schema, want: Expectations) -> Check:
    gaps = []
    for key, expected in want.types.items():
        table, _, column = key.partition(".")
        found = schema.tables.get(table)
        actual = found.columns.get(column) if found else None
        if actual is None or sql_schema.base_type(actual.type) != expected:
            gaps.append(f"{key} should be {expected}" + (f", is {actual.type}" if actual else ""))
    return Check(name="uses the right types", passed=not gaps, detail="; ".join(gaps))


def _nullability(schema: sql_schema.Schema, want: Expectations) -> Check:
    gaps = []
    for key, must_be_not_null in [(k, True) for k in want.required] + [
        (k, False) for k in want.nullable
    ]:
        table, _, column = key.partition(".")
        found = schema.tables.get(table)
        actual = found.columns.get(column) if found else None
        if actual is None or actual.not_null != must_be_not_null:
            gaps.append(f"{key} should be {'NOT NULL' if must_be_not_null else 'nullable'}")
    return Check(name="gets nullability right", passed=not gaps, detail="; ".join(gaps))


async def _models(schema: sql_schema.Schema, output: DbSchemaOutput) -> Check:
    if not output.models.strip():
        return Check(name="models match the DDL", passed=False, detail="no models")
    try:
        described = await sa_models.introspect(output.models)
    except sa_models.ModelsError as exc:
        return Check(name="models match the DDL", passed=False, detail=str(exc))
    problems = sa_models.differences(schema, described)
    return Check(name="models match the DDL", passed=not problems, detail="; ".join(problems[:3]))


def _diagram(schema: sql_schema.Schema, output: DbSchemaOutput) -> Check:
    gaps = [t for t in schema.tables if f"{t} {{" not in output.erd]
    ok = output.erd.startswith("erDiagram") and not gaps
    return Check(
        name="ER diagram shows every table", passed=ok, detail="missing " + ", ".join(gaps)
    )
