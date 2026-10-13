"""Read PostgreSQL schema DDL into a small model, lint it, and draw it as an ERD.

The parser is PostgreSQL's own (``pglast`` wraps libpg_query), so "does it parse?"
has the same answer as on a real server. From the parse tree we build a
:class:`Schema` of tables, columns, keys and indexes, which is what the lint rules,
the ER diagram and the comparison with ORM models work on.

Only schema *definitions* are accepted: ``CREATE TABLE``/``INDEX``/``TYPE ... AS
ENUM``/``EXTENSION``, ``ALTER TABLE ... ADD CONSTRAINT`` and ``COMMENT``. Anything
else (``DROP``, ``INSERT``, ``GRANT``, transactions ...) is reported as a problem,
so a model-written script can never delete or change data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.stream import RawStream

# --------------------------------------------------------------------------- model


@dataclass
class Column:
    name: str
    type: str  # normalised, e.g. "varchar(255)", "numeric(12,2)", "uuid", "text[]"
    not_null: bool = False
    has_default: bool = False
    generated: bool = False  # serial / identity: the database fills it in


@dataclass(frozen=True)
class ForeignKey:
    columns: tuple[str, ...]
    ref_table: str
    ref_columns: tuple[str, ...]  # empty until resolved against the referenced primary key
    on_delete: str = ""

    def label(self) -> str:
        return f"{', '.join(self.columns)} -> {self.ref_table}({', '.join(self.ref_columns)})"


@dataclass
class Table:
    name: str
    columns: dict[str, Column] = field(default_factory=dict)
    primary_key: tuple[str, ...] = ()
    uniques: list[tuple[str, ...]] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[tuple[str, ...]] = field(default_factory=list)
    checks: int = 0
    line: int = 1


@dataclass
class Schema:
    tables: dict[str, Table] = field(default_factory=dict)
    enums: dict[str, list[str]] = field(default_factory=dict)
    extensions: set[str] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)  # parse-level, always blocking
    order: list[str] = field(default_factory=list)  # table creation order

    def unique_sets(self, table: Table) -> list[tuple[str, ...]]:
        return [table.primary_key, *table.uniques] if table.primary_key else list(table.uniques)


@dataclass
class Lint:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class DdlError(ValueError):
    """The text is not valid PostgreSQL (the message says where)."""


# --------------------------------------------------------------------------- types

_ALIASES = {
    "int": "integer",
    "int4": "integer",
    "int2": "smallint",
    "int8": "bigint",
    "bool": "boolean",
    "float8": "double precision",
    "float4": "real",
    "decimal": "numeric",
    "bpchar": "char",
    "character varying": "varchar",
    "character": "char",
    "timestamp with time zone": "timestamptz",
    "timestamp without time zone": "timestamp",
    "time with time zone": "timetz",
    "time without time zone": "time",
}
_SERIAL = {"serial": "integer", "bigserial": "bigint", "smallserial": "smallint"}
_BUILTIN = {
    "integer", "bigint", "smallint", "numeric", "real", "double precision", "boolean", "text",
    "varchar", "char", "uuid", "date", "time", "timetz", "timestamp", "timestamptz", "interval",
    "json", "jsonb", "bytea", "inet", "cidr", "macaddr", "money", "xml", "bit", "varbit",
    "serial", "bigserial", "smallserial", "tsvector", "tsquery", "point", "oid", "name",
}  # fmt: skip
_EXTENSION_TYPES = {"citext", "hstore", "ltree", "vector", "geometry", "geography"}
_FAMILY = {
    "integer": "int", "smallint": "int", "bigint": "int", "serial": "int", "bigserial": "int",
    "smallserial": "int", "text": "text", "varchar": "text", "char": "text", "citext": "text",
}  # fmt: skip


def normalize_type(raw: str) -> str:
    """``Character Varying(255)`` -> ``varchar(255)``; ``int4`` -> ``integer``; spaces tidied."""
    text = re.sub(r"\s+", " ", raw.strip().lower().replace("pg_catalog.", ""))
    text = re.sub(r"\s*,\s*", ",", re.sub(r"\(\s*", "(", re.sub(r"\s*\)", ")", text)))
    array = ""
    while text.endswith("[]"):
        array += "[]"
        text = text[:-2].rstrip()
    base, paren, rest = text.partition("(")
    base = _ALIASES.get(base.strip(), base.strip())
    return f"{base}{paren}{rest}{array}"


def base_type(type_: str) -> str:
    """The type without length, precision or array brackets: ``numeric(12,2)[]`` -> ``numeric``."""
    return type_.partition("(")[0].removesuffix("[]").strip()


def type_family(type_: str) -> str:
    """Types that can be compared in a foreign key: ``serial``, ``integer`` and ``bigint`` match."""
    base = base_type(type_)
    return _FAMILY.get(base, base) + ("[]" if type_.endswith("[]") else "")


# --------------------------------------------------------------------------- parsing

_ALLOWED = (
    ast.CreateStmt,
    ast.IndexStmt,
    ast.CreateEnumStmt,
    ast.CreateExtensionStmt,
    ast.AlterTableStmt,
    ast.CommentStmt,
)


def parse_ddl(sql: str) -> Schema:
    """Parse PostgreSQL DDL into a :class:`Schema`; raises :class:`DdlError` on a syntax error."""
    try:
        statements = parse_sql(sql)
    except ParseError as exc:
        raise DdlError(_locate(sql, str(exc))) from exc
    schema = Schema()
    for raw in statements:
        stmt: Any = raw.stmt
        line = sql.count("\n", 0, raw.stmt_location or 0) + 1
        if isinstance(stmt, ast.CreateStmt):
            _create_table(schema, stmt, line)
        elif isinstance(stmt, ast.IndexStmt):
            _create_index(schema, stmt, line)
        elif isinstance(stmt, ast.CreateEnumStmt):
            _create_enum(schema, stmt)
        elif isinstance(stmt, ast.CreateExtensionStmt):
            schema.extensions.add(str(stmt.extname))
        elif isinstance(stmt, ast.AlterTableStmt):
            _alter_table(schema, stmt, line)
        elif not isinstance(stmt, _ALLOWED):
            kind = type(stmt).__name__.removesuffix("Stmt")
            schema.problems.append(
                f"line {line}: {kind} statements are not allowed; write only CREATE TABLE, "
                "CREATE INDEX, CREATE TYPE ... AS ENUM, CREATE EXTENSION and ALTER TABLE ... ADD"
            )
    _resolve_foreign_keys(schema)
    return schema


def _locate(sql: str, message: str) -> str:
    match = re.search(r"at index (\d+)", message)
    if not match:
        return message
    index = int(match.group(1))
    line = sql.count("\n", 0, index) + 1
    column = index - (sql.rfind("\n", 0, index) + 1) + 1
    return f"{message.replace(match.group(0), '').strip(' ,')} (line {line}, column {column})"


def _names(items: Any) -> tuple[str, ...]:
    return tuple(item.sval for item in items or ())


def _type_text(type_name: Any) -> str:
    return normalize_type(RawStream()(type_name))


def _create_table(schema: Schema, stmt: Any, line: int) -> None:
    name = stmt.relation.relname
    if name in schema.tables:
        schema.problems.append(f"line {line}: table {name} is created twice")
        return
    table = Table(name=name, line=line)
    schema.tables[name] = table
    schema.order.append(name)
    for element in stmt.tableElts or ():
        if isinstance(element, ast.ColumnDef):
            _column(schema, table, element, line)
        elif isinstance(element, ast.Constraint):
            _constraint(table, element, None)


def _column(schema: Schema, table: Table, node: Any, line: int) -> None:
    if node.colname in table.columns:
        schema.problems.append(f"line {line}: column {table.name}.{node.colname} is defined twice")
        return
    type_ = _type_text(node.typeName)
    column = Column(node.colname, type_, generated=base_type(type_) in _SERIAL)
    column.not_null = column.generated
    table.columns[node.colname] = column
    for constraint in node.constraints or ():
        kind = constraint.contype.name
        if kind == "CONSTR_NOTNULL":
            column.not_null = True
        elif kind == "CONSTR_DEFAULT":
            column.has_default = True
        elif kind == "CONSTR_IDENTITY":
            column.generated = column.not_null = True
        else:
            _constraint(table, constraint, node.colname)


def _constraint(table: Table, node: Any, column: str | None) -> None:
    kind = node.contype.name
    keys = (column,) if column else _names(node.keys)
    if kind == "CONSTR_PRIMARY":
        table.primary_key = keys
        for key in keys:
            if key in table.columns:
                table.columns[key].not_null = True
    elif kind == "CONSTR_UNIQUE":
        table.uniques.append(keys)
    elif kind == "CONSTR_CHECK":
        table.checks += 1
    elif kind == "CONSTR_FOREIGN":
        local = (column,) if column else _names(node.fk_attrs)
        action = {"c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT", "r": "RESTRICT"}.get(
            node.fk_del_action, ""
        )
        table.foreign_keys.append(
            ForeignKey(local, node.pktable.relname, _names(node.pk_attrs), action)
        )


def _create_enum(schema: Schema, stmt: Any) -> None:
    schema.enums[stmt.typeName[-1].sval] = [v.sval for v in stmt.vals or ()]


def _create_index(schema: Schema, stmt: Any, line: int) -> None:
    table = schema.tables.get(stmt.relation.relname)
    if table is None:
        schema.problems.append(
            f"line {line}: index on {stmt.relation.relname}, which is not created above it"
        )
        return
    columns = tuple(p.name for p in stmt.indexParams or () if getattr(p, "name", None))
    table.indexes.append(columns)
    if stmt.unique and columns and not stmt.whereClause:
        table.uniques.append(columns)


def _alter_table(schema: Schema, stmt: Any, line: int) -> None:
    table = schema.tables.get(stmt.relation.relname)
    for cmd in stmt.cmds or ():
        if cmd.subtype.name != "AT_AddConstraint" or not isinstance(cmd.def_, ast.Constraint):
            schema.problems.append(
                f"line {line}: only ALTER TABLE ... ADD CONSTRAINT is allowed, "
                f"not {cmd.subtype.name.removeprefix('AT_')}"
            )
        elif table is None:
            schema.problems.append(
                f"line {line}: ALTER TABLE on {stmt.relation.relname}, which is not created above"
            )
        else:
            _constraint(table, cmd.def_, None)


def _resolve_foreign_keys(schema: Schema) -> None:
    """``REFERENCES customers`` with no column list means the primary key of customers."""
    for table in schema.tables.values():
        for index, fk in enumerate(table.foreign_keys):
            target = schema.tables.get(fk.ref_table)
            if not fk.ref_columns and target is not None and target.primary_key:
                table.foreign_keys[index] = ForeignKey(
                    fk.columns, fk.ref_table, target.primary_key, fk.on_delete
                )


# --------------------------------------------------------------------------- lint

_MONEY = re.compile(r"price|amount|total|cost|balance|fee|salary|revenue", re.I)
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def lint(schema: Schema) -> Lint:
    """Blocking errors and non-blocking warnings about the design of a parsed schema."""
    result = Lint(errors=list(schema.problems))
    if not schema.tables:
        result.errors.append("the DDL creates no tables")
    for table in schema.tables.values():
        _lint_table(schema, table, result)
    return result


def _lint_table(schema: Schema, table: Table, result: Lint) -> None:
    name = table.name
    if not table.primary_key:
        result.errors.append(f"table {name} has no primary key")
    for key in table.primary_key:
        if key not in table.columns:
            result.errors.append(f"table {name}: primary key column {key} does not exist")
    for column in table.columns.values():
        _lint_column(schema, table, column, result)
    for unique in table.uniques:
        for column_name in unique:
            if column_name not in table.columns:
                result.errors.append(f"table {name}: unique column {column_name} does not exist")
    for fk in table.foreign_keys:
        _lint_foreign_key(schema, table, fk, result)
    if not _SNAKE.match(name):
        result.warnings.append(f"table {name}: use lower snake_case names")


def _lint_column(schema: Schema, table: Table, column: Column, result: Lint) -> None:
    where = f"{table.name}.{column.name}"
    base = base_type(column.type)
    known = (
        base in _BUILTIN
        or base in schema.enums
        or (base in _EXTENSION_TYPES and base in schema.extensions)
    )
    if not known:
        hint = " (add CREATE EXTENSION)" if base in _EXTENSION_TYPES else ""
        result.errors.append(f"{where}: unknown type {column.type}{hint}")
    if base == "timestamp":
        result.warnings.append(f"{where}: prefer timestamptz over timestamp without time zone")
    if base in {"real", "double precision"} and _MONEY.search(column.name):
        result.warnings.append(f"{where}: use numeric, not {base}, for money")
    if base == "money":
        result.warnings.append(f"{where}: prefer numeric(12,2) over the money type")
    if not _SNAKE.match(column.name):
        result.warnings.append(f"{where}: use lower snake_case names")


def _lint_foreign_key(schema: Schema, table: Table, fk: ForeignKey, result: Lint) -> None:
    where = f"{table.name}({', '.join(fk.columns)})"
    target = schema.tables.get(fk.ref_table)
    if target is None:
        result.errors.append(f"{where} references table {fk.ref_table}, which does not exist")
        return
    if fk.ref_table != table.name and schema.order.index(fk.ref_table) > schema.order.index(
        table.name
    ):
        result.errors.append(
            f"{where} references {fk.ref_table}, which is created later: create parent tables first"
        )
    if not fk.ref_columns:
        result.errors.append(f"{where} references {fk.ref_table}, which has no primary key")
        return
    if len(fk.columns) != len(fk.ref_columns):
        result.errors.append(
            f"{where} has {len(fk.columns)} column(s) but references {len(fk.ref_columns)}"
        )
        return
    for local, remote in zip(fk.columns, fk.ref_columns, strict=True):
        if local not in table.columns:
            result.errors.append(f"{where}: column {local} does not exist")
        elif remote not in target.columns:
            result.errors.append(f"{where}: {fk.ref_table}.{remote} does not exist")
        elif type_family(table.columns[local].type) != type_family(target.columns[remote].type):
            result.errors.append(
                f"{table.name}.{local} is {table.columns[local].type} but references "
                f"{fk.ref_table}.{remote} which is {target.columns[remote].type}"
            )
    if tuple(fk.ref_columns) not in [tuple(u) for u in schema.unique_sets(target)]:
        result.errors.append(
            f"{where} references {fk.ref_table}({', '.join(fk.ref_columns)}), "
            "which is neither its primary key nor unique"
        )
    if not _indexed(table, fk.columns):
        result.warnings.append(f"{where} is a foreign key with no index starting with it")


def _indexed(table: Table, columns: tuple[str, ...]) -> bool:
    candidates = [table.primary_key, *table.uniques, *table.indexes]
    return any(c[: len(columns)] == columns for c in candidates if c)


# --------------------------------------------------------------------------- ER diagram


def to_mermaid(schema: Schema) -> str:
    """A Mermaid ``erDiagram``: one box per table, one line per foreign key."""
    lines = ["erDiagram"]
    for table in schema.tables.values():
        for fk in table.foreign_keys:
            if fk.ref_table not in schema.tables:
                continue
            one_to_one = tuple(fk.columns) in [tuple(u) for u in schema.unique_sets(table)]
            optional = not all(table.columns[c].not_null for c in fk.columns if c in table.columns)
            left = "|o" if optional else "||"
            right = "o|" if one_to_one else "o{"
            lines.append(f'    {fk.ref_table} {left}--{right} {table.name} : "{fk.columns[0]}"')
    for table in schema.tables.values():
        lines.append(f"    {table.name} {{")
        fk_columns = {c for fk in table.foreign_keys for c in fk.columns}
        unique_columns = {u[0] for u in table.uniques if len(u) == 1}
        for column in table.columns.values():
            keys = []
            if column.name in table.primary_key:
                keys.append("PK")
            if column.name in fk_columns:
                keys.append("FK")
            if column.name in unique_columns and column.name not in table.primary_key:
                keys.append("UK")
            lines.append(
                f"        {_mermaid_type(column.type)} {column.name}"
                + (f" {', '.join(keys)}" if keys else "")
            )
        lines.append("    }")
    return "\n".join(lines) + "\n"


def _mermaid_type(type_: str) -> str:
    """Mermaid attribute types are single words: ``numeric(12,2)`` -> ``numeric``."""
    return base_type(type_).replace(" ", "_") + ("_array" if type_.endswith("[]") else "")
