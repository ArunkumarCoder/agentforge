"""PostgreSQL DDL helpers: parsing, lint rules, the ER diagram and the ORM comparison."""
# ruff: noqa: E501

import pytest

from agentforge.tools import sa_models, sql_schema
from agentforge.tools.sql_schema import DdlError, normalize_type, parse_ddl
from tests.agents.dbschema_refs import REFERENCE_DDL, REFERENCE_MODELS

SHOP = parse_ddl(REFERENCE_DDL["shop"])


def lint(sql: str) -> sql_schema.Lint:
    return sql_schema.lint(parse_ddl(sql))


# --------------------------------------------------------------------------- types


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("INT4", "integer"),
        ("Character Varying(255)", "varchar(255)"),
        ("numeric( 12 , 2 )", "numeric(12,2)"),
        ("timestamp with time zone", "timestamptz"),
        ("pg_catalog.int8", "bigint"),
        ("text[]", "text[]"),
        ("bool", "boolean"),
    ],
)
def test_normalize_type(raw: str, expected: str) -> None:
    assert normalize_type(raw) == expected


def test_type_families_make_fks_comparable() -> None:
    assert sql_schema.type_family("serial") == sql_schema.type_family("integer")
    assert sql_schema.type_family("varchar(10)") == sql_schema.type_family("text")
    assert sql_schema.type_family("uuid") != sql_schema.type_family("integer")
    assert sql_schema.type_family("text[]") == "text[]"
    assert sql_schema.base_type("numeric(12,2)[]") == "numeric"


# --------------------------------------------------------------------------- parsing


def test_parses_tables_columns_keys_and_indexes() -> None:
    orders = SHOP.tables["orders"]
    assert SHOP.order == ["customers", "products", "orders", "order_items"]
    assert orders.primary_key == ("id",)
    assert orders.columns["total"].type == "numeric(12,2)"
    assert orders.columns["status"].type == "order_status"
    assert orders.columns["id"].not_null  # primary key columns are NOT NULL
    assert orders.foreign_keys[0].ref_table == "customers"
    assert orders.foreign_keys[0].ref_columns == ("id",)
    assert orders.foreign_keys[0].on_delete == "RESTRICT"
    assert orders.indexes == [("customer_id",)]
    assert SHOP.tables["order_items"].uniques == [("order_id", "product_id")]
    assert SHOP.tables["products"].checks == 2
    assert SHOP.enums == {"order_status": ["pending", "paid", "shipped", "cancelled"]}
    assert "pgcrypto" in SHOP.extensions


def test_inline_and_table_level_constraints_are_equivalent() -> None:
    inline = parse_ddl("CREATE TABLE a (id int PRIMARY KEY, e text UNIQUE, p int REFERENCES a);")
    table = parse_ddl(
        "CREATE TABLE a (id int, e text, p int, PRIMARY KEY (id), UNIQUE (e), FOREIGN KEY (p) REFERENCES a (id));"
    )
    for schema in (inline, table):
        a = schema.tables["a"]
        assert a.primary_key == ("id",)
        assert a.uniques == [("e",)]
        assert a.foreign_keys[0].ref_columns == ("id",)  # bare REFERENCES a means a's primary key


def test_serial_identity_and_defaults() -> None:
    schema = parse_ddl(
        "CREATE TABLE a (id bigserial PRIMARY KEY, n int GENERATED ALWAYS AS IDENTITY, d int DEFAULT 5, x int);"
    )
    columns = schema.tables["a"].columns
    assert columns["id"].generated and columns["id"].not_null
    assert columns["n"].generated and columns["n"].not_null
    assert columns["d"].has_default and not columns["d"].not_null
    assert not columns["x"].not_null


def test_alter_table_add_constraint_and_unique_index() -> None:
    schema = parse_ddl(
        "CREATE TABLE a (id int PRIMARY KEY);\n"
        "CREATE TABLE b (id int PRIMARY KEY, a_id int);\n"
        "ALTER TABLE b ADD CONSTRAINT fk FOREIGN KEY (a_id) REFERENCES a (id);\n"
        "CREATE UNIQUE INDEX ux ON b (a_id);\n"
        "CREATE INDEX partial ON b (a_id) WHERE a_id > 0;\n"
    )
    b = schema.tables["b"]
    assert b.foreign_keys[0].ref_table == "a"
    assert b.uniques == [("a_id",)]
    assert b.indexes == [("a_id",), ("a_id",)]
    assert schema.problems == []


def test_syntax_errors_say_where() -> None:
    with pytest.raises(DdlError, match=r"line 2, column 9"):
        parse_ddl("CREATE TABLE x (\n id int,, )")
    with pytest.raises(DdlError):
        parse_ddl("this is not sql at all")


@pytest.mark.parametrize(
    ("sql", "needle"),
    [
        ("DROP TABLE a;", "Drop statements are not allowed"),
        ("INSERT INTO a VALUES (1);", "Insert statements"),
        ("BEGIN;", "Transaction statements"),
        ("GRANT ALL ON a TO bob;", "Grant statements"),
        (
            "CREATE TABLE a (id int PRIMARY KEY);\nALTER TABLE a DROP COLUMN id;",
            "only ALTER TABLE ... ADD CONSTRAINT",
        ),
        ("ALTER TABLE ghost ADD CONSTRAINT c CHECK (true);", "ghost, which is not created above"),
        ("CREATE INDEX i ON ghost (a);", "ghost, which is not created above"),
        ("CREATE TABLE a (id int);\nCREATE TABLE a (id int);", "table a is created twice"),
        ("CREATE TABLE a (id int, id text);", "a.id is defined twice"),
    ],
)
def test_problems_are_reported_not_executed(sql: str, needle: str) -> None:
    assert any(needle in p for p in parse_ddl(sql).problems)


# --------------------------------------------------------------------------- lint


def test_reference_schemas_lint_clean() -> None:
    for sql in REFERENCE_DDL.values():
        found = lint(sql)
        assert (found.errors, found.warnings) == ([], [])


def test_empty_ddl_and_missing_primary_key() -> None:
    assert lint("CREATE EXTENSION pgcrypto;").errors == ["the DDL creates no tables"]
    assert "table a has no primary key" in lint("CREATE TABLE a (x int);").errors


def test_primary_and_unique_columns_must_exist() -> None:
    found = lint("CREATE TABLE a (id int, PRIMARY KEY (nope), UNIQUE (ghost));").errors
    assert "table a: primary key column nope does not exist" in found
    assert "table a: unique column ghost does not exist" in found


def test_foreign_key_rules() -> None:
    parents = "CREATE TABLE p (id uuid PRIMARY KEY, code text UNIQUE, n int);\n"
    assert (
        "references table q, which does not exist"
        in lint(
            parents + "CREATE TABLE c (id int PRIMARY KEY, q_id uuid REFERENCES q(id));"
        ).errors[0]
    )
    mismatch = lint(
        parents + "CREATE TABLE c (id int PRIMARY KEY, p_id integer REFERENCES p(id));"
    ).errors
    assert "c.p_id is integer but references p.id which is uuid" in mismatch
    not_unique = lint(
        parents + "CREATE TABLE c (id int PRIMARY KEY, p_n int REFERENCES p(n));"
    ).errors
    assert any("neither its primary key nor unique" in e for e in not_unique)
    assert (
        lint(
            parents + "CREATE TABLE c (id int PRIMARY KEY, p_code text REFERENCES p(code));"
        ).errors
        == []
    )
    missing_col = lint(
        parents + "CREATE TABLE c (id int PRIMARY KEY, p_id uuid REFERENCES p(nope));"
    ).errors
    assert "c(p_id): p.nope does not exist" in missing_col
    no_pk = lint(
        "CREATE TABLE p (x int);\nCREATE TABLE c (id int PRIMARY KEY, p_id int REFERENCES p);"
    ).errors
    assert "c(p_id) references p, which has no primary key" in no_pk


def test_creation_order_and_self_references() -> None:
    late = lint(
        "CREATE TABLE c (id int PRIMARY KEY, p int REFERENCES p(id));\nCREATE TABLE p (id int PRIMARY KEY);"
    )
    assert any("which is created later" in e for e in late.errors)
    tree = lint("CREATE TABLE node (id int PRIMARY KEY, parent_id int REFERENCES node(id));")
    assert tree.errors == []


def test_composite_foreign_key_checks() -> None:
    sql = (
        "CREATE TABLE p (a int, b int, PRIMARY KEY (a, b));\n"
        "CREATE TABLE c (id int PRIMARY KEY, a int, b int, FOREIGN KEY (a, b) REFERENCES p (a, b));"
    )
    assert lint(sql).errors == []


def test_unknown_types_and_extension_types() -> None:
    assert (
        "a.x: unknown type nonsense"
        in lint("CREATE TABLE a (id int PRIMARY KEY, x nonsense);").errors
    )
    without = lint("CREATE TABLE a (id int PRIMARY KEY, x citext);").errors
    assert "a.x: unknown type citext (add CREATE EXTENSION)" in without
    assert (
        lint("CREATE EXTENSION citext;\nCREATE TABLE a (id int PRIMARY KEY, x citext);").errors
        == []
    )
    assert (
        lint("CREATE TYPE mood AS ENUM ('a');\nCREATE TABLE a (id int PRIMARY KEY, m mood);").errors
        == []
    )


def test_style_findings_are_warnings() -> None:
    found = lint(
        'CREATE TABLE Items (id int PRIMARY KEY, "Price" real, ts timestamp, cash money, owner int);\n'
        "CREATE TABLE d (id int PRIMARY KEY, item_id int REFERENCES Items(id));"
    )
    text = "\n".join(found.warnings)
    assert found.errors == []
    assert "use lower snake_case names" in text
    assert "use numeric, not real, for money" in text
    assert "prefer timestamptz" in text
    assert "prefer numeric(12,2) over the money type" in text
    assert "d(item_id) is a foreign key with no index" in text


def test_foreign_key_covered_by_composite_or_unique_index_is_fine() -> None:
    sql = (
        "CREATE TABLE p (id int PRIMARY KEY);\n"
        "CREATE TABLE c (a int REFERENCES p(id), b int, PRIMARY KEY (a, b));"
    )
    assert lint(sql).warnings == []


# --------------------------------------------------------------------------- ER diagram


def test_mermaid_shows_tables_keys_and_cardinality() -> None:
    erd = sql_schema.to_mermaid(SHOP)
    assert erd.startswith("erDiagram\n")
    assert 'customers ||--o{ orders : "customer_id"' in erd
    assert "        uuid id PK" in erd
    assert "        uuid customer_id FK" in erd
    assert "        varchar email UK" in erd
    assert "        numeric price" in erd  # no precision in the type: Mermaid wants one word
    assert "        order_status status" in erd


def test_mermaid_optional_one_to_one_and_arrays() -> None:
    schema = parse_ddl(
        "CREATE TABLE a (id int PRIMARY KEY, tags text[]);\n"
        "CREATE TABLE b (id int PRIMARY KEY, a_id int NOT NULL UNIQUE REFERENCES a(id));\n"
        "CREATE TABLE c (id int PRIMARY KEY, a_id int REFERENCES a(id), g int REFERENCES zzz(id));"
    )
    erd = sql_schema.to_mermaid(schema)
    assert 'a ||--o| b : "a_id"' in erd  # unique and required: one to one
    assert 'a |o--o{ c : "a_id"' in erd  # nullable: optional parent
    assert "text_array tags" in erd
    assert "zzz" not in erd.split("c {")[0]  # a dangling reference draws no line


# --------------------------------------------------------------------------- ORM models


async def test_models_match_the_reference_ddl() -> None:
    for name, sql in REFERENCE_DDL.items():
        described = await sa_models.introspect(REFERENCE_MODELS[name])
        assert sa_models.differences(parse_ddl(sql), described) == [], name


async def test_introspect_describes_tables() -> None:
    described = await sa_models.introspect(REFERENCE_MODELS["shop"])
    items = described["order_items"]
    assert items.primary_key == ("id",)
    assert ("order_id", "orders", "id") in items.foreign_keys
    assert items.columns["quantity"] is False  # not nullable


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("def broken(:\n", "SyntaxError"),
        ("x = 1\n", "declarative base class named Base"),
        ("import not_a_module\n", "ModuleNotFoundError"),
        (
            "from sqlalchemy import ForeignKey\nfrom sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column\n"
            "class Base(DeclarativeBase):\n    pass\n"
            "class A(Base):\n    __tablename__ = 'a'\n    id: Mapped[int] = mapped_column(primary_key=True)\n"
            "    b_id: Mapped[int] = mapped_column(ForeignKey('b.id'))\n",
            "NoReferencedTableError",
        ),
    ],
)
async def test_models_that_do_not_load_are_reported(source: str, needle: str) -> None:
    with pytest.raises(sa_models.ModelsError, match=needle):
        await sa_models.introspect(source)


async def test_model_differences_cover_keys_and_columns() -> None:
    ddl = parse_ddl(REFERENCE_DDL["shop"])
    models = await sa_models.introspect(REFERENCE_MODELS["shop"])
    assert sa_models.differences(ddl, models) == []
    models["customers"].primary_key = ("email",)
    models["products"].foreign_keys.append(("name", "customers", "id"))
    models["orders"].columns["total"] = True
    del models["order_items"]
    models["ghost"] = sa_models.ModelTable(columns={"id": False}, primary_key=("id",))
    found = "\n".join(sa_models.differences(ddl, models))
    assert "table order_items is in the DDL but has no model" in found
    assert "model for table ghost has no CREATE TABLE" in found
    assert "customers primary key is (id) in the DDL but (email) in the model" in found
    assert "products foreign keys differ" in found
    assert "orders.total is NOT NULL in the DDL but nullable in the model" in found


async def test_timeouts_are_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sa_models, "TIMEOUT_S", 0.05)
    slow = "import time\ntime.sleep(5)\n"
    with pytest.raises(sa_models.ModelsError, match="took longer"):
        await sa_models.introspect(slow)
