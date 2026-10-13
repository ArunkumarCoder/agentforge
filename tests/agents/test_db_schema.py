"""Database Schema agent: DDL parsing, lint, model comparison, optional Postgres run, golden cases.

FakeProvider plays the model with scripted JSON answers. Golden-case tests also prove that
each case's scoring fails when something is wrong, so the evals measure what they claim to.
"""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from agentforge.agents.db_schema import DbSchemaAgent, DbSchemaInput, DbSchemaOutput
from agentforge.agents.db_schema.scoring import load_cases, score
from agentforge.core import ErrorKind
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools import postgres
from tests.agents.dbschema_refs import REFERENCE_DDL, REFERENCE_MODELS

CASES = {c.name: c for c in load_cases()}
REAL_URL = postgres.configured_url()  # read before the autouse fixture hides it from other tests
BLOG = CASES["blog"]


@pytest.fixture(autouse=True)
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(postgres.URL_ENV, raising=False)


def answer(
    name: str = "blog", ddl: str | None = None, models: str | None = None, **extra: Any
) -> str:
    base = {
        "summary": "Users, posts, comments and tags.",
        "ddl": ddl if ddl is not None else REFERENCE_DDL[name],
        "models": models if models is not None else REFERENCE_MODELS[name],
        "assumptions": ["emails are case sensitive"],
    }
    return json.dumps({**base, **extra})


async def run(llm: FakeProvider, **data: Any) -> DbSchemaOutput:
    result = await DbSchemaAgent(llm).arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("db-schema") is DbSchemaAgent
    assert "database" in DbSchemaAgent.card().tags


def test_input_defaults_and_rules() -> None:
    data = DbSchemaInput(requirements="Users and their posts")
    assert (data.id_style.value, data.orm_models) == ("uuid", True)
    with pytest.raises(ValueError):
        DbSchemaInput(requirements="short")


async def test_designs_checks_and_fills_the_report() -> None:
    llm = FakeProvider(script=[answer()])
    output = await run(llm, **BLOG.input)
    assert output.valid is True
    assert output.tables == ["users", "posts", "comments", "tags", "post_tags"]
    assert output.executed is None  # no server configured
    assert output.warnings == []
    assert output.erd.startswith("erDiagram")
    assert 'users ||--o{ posts : "author_id"' in output.erd
    assert "post_tags {" in output.erd
    system, user = (m.content for m in llm.requests[0].messages[:2])
    assert '"ddl"' in system
    assert "PostgreSQL" in system
    assert "gen_random_uuid()" in user
    assert "write them" in user
    assert "blogging platform" in user


async def test_bigint_keys_and_no_models() -> None:
    ddl = (
        "CREATE TABLE notes (\n"
        "    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n"
        "    body text NOT NULL\n);\n"
    )
    llm = FakeProvider(script=[answer(ddl=ddl, models="")])
    output = await run(
        llm,
        requirements="A table of notes with a body.",
        id_style="bigint",
        orm_models=False,
        instructions="Keep it tiny",
    )
    user = llm.requests[0].messages[1].content
    assert "GENERATED ALWAYS AS IDENTITY" in user
    assert "not wanted" in user
    assert "Keep it tiny" in user
    assert output.models == ""
    assert output.tables == ["notes"]


async def test_models_dropped_when_not_wanted_even_if_sent() -> None:
    ddl = "CREATE TABLE notes (id uuid PRIMARY KEY, body text NOT NULL);\n"
    output = await run(
        FakeProvider(script=[answer(ddl=ddl, models="junk")]),
        requirements="A table of notes with a body.",
        orm_models=False,
    )
    assert output.models == ""


# --------------------------------------------------------------------------- repairs


async def feedback_for(bad: str, **data: Any) -> str:
    llm = FakeProvider(script=[bad, answer()])
    await run(llm, **(data or BLOG.input))
    return llm.requests[1].messages[-1].content


async def test_syntax_error_is_located() -> None:
    text = await feedback_for(answer(ddl="CREATE TABLE users (\n  id uuid,, \n);"))
    assert "not valid PostgreSQL" in text
    assert "line 2" in text


async def test_forbidden_statements_are_refused() -> None:
    text = await feedback_for(
        answer(ddl=REFERENCE_DDL["blog"] + "\nDROP TABLE users;\nINSERT INTO tags VALUES (1);")
    )
    assert "Drop statements are not allowed" in text
    assert "Insert statements are not allowed" in text


async def test_schema_rule_violations_are_listed() -> None:
    bad = REFERENCE_DDL["blog"].replace(
        "author_id uuid NOT NULL REFERENCES users (id)",
        "author_id uuid NOT NULL REFERENCES people (id)",
        1,
    )
    text = await feedback_for(answer(ddl=bad))
    assert "breaks schema rules" in text
    assert "references table people, which does not exist" in text


async def test_parents_must_come_first() -> None:
    ddl = REFERENCE_DDL["blog"]
    users = ddl[ddl.index("CREATE TABLE users") : ddl.index("CREATE TABLE posts")]
    swapped = ddl.replace(users, "") + "\n" + users
    text = await feedback_for(answer(ddl=swapped))
    assert "is created later" in text


async def test_models_must_exist_and_load() -> None:
    assert "`models` is empty" in await feedback_for(answer(models="  "))
    assert "do not load" in await feedback_for(answer(models="this is not python("))
    assert "declarative base class named Base" in await feedback_for(answer(models="x = 1\n"))


async def test_model_differences_are_reported() -> None:
    models = REFERENCE_MODELS["blog"]
    no_join = models[: models.index("class PostTag(Base)")]
    text = await feedback_for(answer(models=no_join))
    assert "The models do not match the DDL" in text
    assert "table post_tags is in the DDL but has no model" in text
    nullable = models.replace(
        "title: Mapped[str] = mapped_column(Text",
        "title: Mapped[str | None] = mapped_column(Text, nullable=True",
        1,
    )
    assert "posts.title is NOT NULL in the DDL but nullable in the model" in await feedback_for(
        answer(models=nullable)
    )
    renamed = models.replace("    slug:", "    web_slug:")
    text = await feedback_for(answer(models=renamed))
    assert "model posts lacks column(s): slug" in text
    assert "has column(s) not in the DDL: web_slug" in text


async def test_a_schema_that_stays_broken_is_an_output_error() -> None:
    bad = answer(ddl="CREATE TABLE a (id int);")  # no primary key
    result = await DbSchemaAgent(FakeProvider(script=[bad, bad, bad])).arun(BLOG.input)
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "table a has no primary key" in result.error.message


async def test_warnings_do_not_block() -> None:
    ddl = "CREATE TABLE events (id uuid PRIMARY KEY, happened timestamp NOT NULL);\n"
    output = await run(
        FakeProvider(script=[answer(ddl=ddl, models="")]),
        requirements="A table of events.",
        orm_models=False,
    )
    assert output.warnings == [
        "events.happened: prefer timestamptz over timestamp without time zone"
    ]


# --------------------------------------------------------------------------- real database


def fake_psql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, stderr: str = ""
) -> Path:
    script = tmp_path / "psql"
    script.write_text(f"#!/bin/sh\ncat <<'EOF' >&2\n{stderr}\nEOF\nexit {exit_code}\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv(postgres.URL_ENV, "postgresql://localhost/scratch")
    return script


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
async def test_server_acceptance_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_psql(tmp_path, monkeypatch, 0)
    assert postgres.available()
    output = await run(FakeProvider(script=[answer()]), **BLOG.input)
    assert output.executed is True


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
async def test_server_rejection_goes_back_to_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_psql(tmp_path, monkeypatch, 3, 'psql:schema.sql:7: ERROR:  type "citext" does not exist')
    llm = FakeProvider(script=[answer(), answer()])
    result = await DbSchemaAgent(llm).arun(BLOG.input)
    assert result.error is not None  # the fake server never accepts
    text = llm.requests[1].messages[-1].content
    assert "PostgreSQL rejected the DDL" in text
    assert "line 4 of your DDL" in text
    assert 'type "citext" does not exist' in text


def test_postgres_is_off_without_a_url_or_psql(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not postgres.available()
    monkeypatch.setenv(postgres.URL_ENV, "postgresql://x/y")
    monkeypatch.setenv("PATH", "")
    assert not postgres.available()


async def test_execute_without_configuration_says_so() -> None:
    result = await postgres.execute_ddl("CREATE TABLE a (id int);")
    assert not result.ok
    assert "no database URL" in result.error


@pytest.mark.integration
@pytest.mark.skipif(
    REAL_URL is None or shutil.which("psql") is None,
    reason=f"set {postgres.URL_ENV} and install psql",
)
async def test_reference_schemas_run_on_a_real_postgres() -> None:
    for name, sql in REFERENCE_DDL.items():
        result = await postgres.execute_ddl(sql, REAL_URL)
        assert result.ok, f"{name}: {result.error}"
    bad = await postgres.execute_ddl(
        "CREATE TABLE a (id int PRIMARY KEY);\nCREATE TABLE b (a_id uuid REFERENCES a(id));",
        REAL_URL,
    )
    assert not bad.ok
    assert "line 2 of your DDL" in bad.error


# --------------------------------------------------------------------------- golden cases


def test_five_cases_with_unique_names() -> None:
    assert sorted(CASES) == ["blog", "booking", "saas", "school", "shop"]


@pytest.mark.parametrize("name", sorted(CASES))
async def test_reference_schema_scores_full_marks(name: str) -> None:
    case = CASES[name]
    output = await run(FakeProvider(script=[answer(name)]), **case.input)
    card = await score(case, output)
    assert card.passed, [c for c in card.checks if not c.passed]
    assert card.score == 1.0


async def scored(
    name: str, ddl_edit: Any = None, models: str | None = None, erd: str | None = None
) -> dict[str, bool]:
    case = CASES[name]
    ddl = REFERENCE_DDL[name]
    if ddl_edit:
        ddl = ddl_edit(ddl)
        assert ddl != REFERENCE_DDL[name]
    output = DbSchemaOutput(
        summary="edited reference",
        ddl=ddl,
        models=REFERENCE_MODELS[name] if models is None else models,
        erd=erd
        if erd is not None
        else "erDiagram\n"
        + "".join(
            f"    {t} {{\n    }}\n"
            for t in [
                "users",
                "posts",
                "comments",
                "tags",
                "post_tags",
                "customers",
                "products",
                "orders",
                "order_items",
                "students",
                "teachers",
                "courses",
                "enrollments",
                "rooms",
                "guests",
                "bookings",
                "organizations",
                "memberships",
                "projects",
                "audit_log",
            ]
        ),
    )
    return {c.name: c.passed for c in (await score(case, output)).checks}


async def test_scoring_notices_unparseable_ddl() -> None:
    case = CASES["blog"]
    card = await score(
        case, DbSchemaOutput(summary="broken output", ddl="CREATE TABLE ((( x y z w v")
    )
    assert [c.name for c in card.checks] == ["DDL parses"]
    assert not card.checks[0].passed


async def test_scoring_notices_missing_tables_columns_and_scope_creep() -> None:
    assert not (await scored("blog", lambda d: d.replace("post_tags", "post_tagz")))[
        "has every table"
    ]
    assert not (await scored("blog", lambda d: d.replace("display_name text NOT NULL,", "")))[
        "has the required columns"
    ]
    extra = "".join(f"CREATE TABLE extra{i} (id uuid PRIMARY KEY);\n" for i in range(4))
    assert not (await scored("blog", lambda d: d + extra, models=REFERENCE_MODELS["blog"]))[
        "stays in scope"
    ]


async def test_scoring_notices_missing_keys_uniques_and_checks() -> None:
    no_fk = await scored(
        "shop",
        lambda d: d.replace(
            "order_id uuid NOT NULL REFERENCES orders (id) ON DELETE CASCADE",
            "order_id uuid NOT NULL",
        ),
    )
    assert not no_fk["has the foreign keys"]
    no_unique = await scored(
        "school", lambda d: d.replace("UNIQUE (student_id, course_id)", "UNIQUE (student_id)")
    )
    assert not no_unique["enforces uniqueness"]
    no_checks = await scored(
        "booking",
        lambda d: d.replace(
            "    CHECK (ends_at > starts_at)", "    UNIQUE (room_id, starts_at)"
        ).replace(",\n    CHECK (capacity > 0)", ""),
    )
    assert not no_checks["has CHECK constraints"]


async def test_scoring_notices_wrong_types_nullability_and_enums() -> None:
    assert not (
        await scored("shop", lambda d: d.replace("price numeric(10,2)", "price double precision"))
    )["uses the right types"]
    assert not (
        await scored(
            "blog",
            lambda d: d.replace("published_at timestamptz,", "published_at timestamptz NOT NULL,"),
        )
    )["gets nullability right"]
    assert not (await scored("saas", lambda d: d.replace("'owner', ", "")))["declares the enums"]


async def test_scoring_notices_missing_or_mismatched_models_and_diagram() -> None:
    assert not (await scored("blog", models=""))["models match the DDL"]
    assert not (await scored("blog", models="x = 1\n"))["models match the DDL"]
    models = (
        REFERENCE_MODELS["blog"].replace('"slug"', '"slugx"')
        if '"slug"' in REFERENCE_MODELS["blog"]
        else REFERENCE_MODELS["blog"].replace("slug:", "slugx:")
    )
    assert not (await scored("blog", models=models))["models match the DDL"]
    assert not (await scored("blog", erd="erDiagram\n"))["ER diagram shows every table"]
