"""Reference schemas that score full marks on the Database Schema golden cases.

A tiny table DSL generates both the PostgreSQL DDL and the SQLAlchemy 2.0 models, so
each reference reads like a design and the two halves cannot drift apart. The tests use
them as the model's perfect answer and to prove the cases measure what they claim to.
"""

import re
from dataclasses import dataclass, field

UUID_PK = "gen_random_uuid()"


@dataclass
class Col:
    name: str
    type: str
    null: bool = False
    unique: bool = False
    default: str | None = None
    fk: str | None = None  # "table.column"
    on_delete: str | None = None
    identity: bool = False


@dataclass
class Table:
    name: str
    cols: list[Col]
    pk: tuple[str, ...] = ("id",)
    unique: list[tuple[str, ...]] = field(default_factory=list)
    checks: list[str] = field(default_factory=list)


def c(name: str, type_: str, *flags: str, **kw: str) -> Col:
    return Col(
        name,
        type_,
        null="null" in flags,
        unique="unique" in flags,
        identity="identity" in flags,
        **kw,
    )


def uuid_id() -> Col:
    return c("id", "uuid", default=UUID_PK)


def created() -> Col:
    return c("created_at", "timestamptz", default="now()")


def ref(name: str, target: str, *flags: str, on_delete: str = "CASCADE") -> Col:
    return c(name, "uuid", *flags, fk=target, on_delete=on_delete)


@dataclass
class Design:
    tables: list[Table]
    enums: dict[str, list[str]] = field(default_factory=dict)


# --------------------------------------------------------------------------- DDL


def ddl(design: Design) -> str:
    out = ["CREATE EXTENSION IF NOT EXISTS pgcrypto;", ""]
    for name, values in design.enums.items():
        out.append(f"CREATE TYPE {name} AS ENUM ({', '.join(repr(v) for v in values)});")
    if design.enums:
        out.append("")
    for table in design.tables:
        lines = []
        for col in table.cols:
            line = f"    {col.name} {col.type}"
            if col.identity:
                line += " GENERATED ALWAYS AS IDENTITY"
            if not col.null:
                line += " NOT NULL"
            if col.unique:
                line += " UNIQUE"
            if col.default:
                line += f" DEFAULT {col.default}"
            if col.fk:
                target, column = col.fk.split(".")
                line += f" REFERENCES {target} ({column})"
                line += f" ON DELETE {col.on_delete}" if col.on_delete else ""
            lines.append(line)
        lines.append(f"    PRIMARY KEY ({', '.join(table.pk)})")
        lines += [f"    UNIQUE ({', '.join(u)})" for u in table.unique]
        lines += [f"    CHECK ({check})" for check in table.checks]
        out.append(f"CREATE TABLE {table.name} (\n" + ",\n".join(lines) + "\n);")
        for col in table.cols:
            covered = [table.pk, *table.unique]
            if col.fk and not any(key[0] == col.name for key in covered):
                out.append(f"CREATE INDEX ix_{table.name}_{col.name} ON {table.name} ({col.name});")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- models

_PY = {
    "uuid": ("uuid.UUID", "Uuid"),
    "text": ("str", "Text"),
    "integer": ("int", "Integer"),
    "bigint": ("int", "BigInteger"),
    "boolean": ("bool", "Boolean"),
    "date": ("date", "Date"),
    "timestamptz": ("datetime", "DateTime(timezone=True)"),
    "jsonb": ("dict[str, Any]", "JSONB"),
}


def _sa_type(col: Col, design: Design) -> tuple[str, str]:
    if col.type in design.enums:
        values = ", ".join(repr(v) for v in design.enums[col.type])
        return "str", f'Enum({values}, name="{col.type}")'
    if col.type.startswith("varchar("):
        return "str", f"String({col.type[8:-1]})"
    if col.type.startswith("numeric("):
        return "Decimal", f"Numeric({col.type[8:-1]})"
    return _PY[col.type]


def class_name(table: str) -> str:
    words = table.split("_")
    last = words[-1]
    if last.endswith("ies"):
        last = last[:-3] + "y"
    elif last.endswith("s") and not last.endswith("ss"):
        last = last[:-1]
    return "".join(w.capitalize() for w in [*words[:-1], last])


def models(design: Design) -> str:
    out = [
        "import uuid",
        "from datetime import date, datetime",
        "from decimal import Decimal",
        "from typing import Any",
        "",
        "from sqlalchemy import (",
        "    BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey, Identity,",
        "    Integer, Numeric, String, Text, UniqueConstraint, Uuid, text,",
        ")",
        "from sqlalchemy.dialects.postgresql import JSONB",
        "from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column",
        "",
        "",
        "class Base(DeclarativeBase):",
        "    pass",
    ]
    for table in design.tables:
        out += [
            "",
            "",
            f"class {class_name(table.name)}(Base):",
            f'    __tablename__ = "{table.name}"',
        ]
        args = [f"UniqueConstraint({', '.join(map(repr, u))})" for u in table.unique]
        args += [f"CheckConstraint({check!r})" for check in table.checks]
        if args:
            out.append(f"    __table_args__ = ({', '.join(args)},)")
        out.append("")
        for col in table.cols:
            py, sa = _sa_type(col, design)
            parts = [sa]
            if col.fk:
                target, column = col.fk.split(".")
                action = f', ondelete="{col.on_delete}"' if col.on_delete else ""
                parts.append(f'ForeignKey("{target}.{column}"{action})')
            if col.identity:
                parts.append("Identity(always=True)")
            if col.name in table.pk:
                parts.append("primary_key=True")
            if col.unique:
                parts.append("unique=True")
            if col.default:
                parts.append(f'server_default=text("{col.default}")')
            if col.null:
                parts.append("nullable=True")
            annotation = f"{py} | None" if col.null else py
            out.append(f"    {col.name}: Mapped[{annotation}] = mapped_column({', '.join(parts)})")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- designs


def blog() -> Design:
    return Design(
        [
            Table(
                "users",
                [
                    uuid_id(),
                    c("email", "varchar(255)", "unique"),
                    c("display_name", "text"),
                    created(),
                ],
            ),
            Table(
                "posts",
                [
                    uuid_id(),
                    ref("author_id", "users.id"),
                    c("title", "text"),
                    c("slug", "varchar(200)", "unique"),
                    c("body", "text"),
                    c("published_at", "timestamptz", "null"),
                    created(),
                ],
            ),
            Table(
                "comments",
                [
                    uuid_id(),
                    ref("post_id", "posts.id"),
                    ref("author_id", "users.id", on_delete="RESTRICT"),
                    c("body", "text"),
                    created(),
                ],
            ),
            Table("tags", [uuid_id(), c("name", "varchar(50)", "unique")]),
            Table(
                "post_tags",
                [ref("post_id", "posts.id"), ref("tag_id", "tags.id")],
                pk=("post_id", "tag_id"),
            ),
        ]
    )


def shop() -> Design:
    return Design(
        [
            Table(
                "customers",
                [uuid_id(), c("email", "varchar(255)", "unique"), c("name", "text"), created()],
            ),
            Table(
                "products",
                [
                    uuid_id(),
                    c("sku", "varchar(40)", "unique"),
                    c("name", "text"),
                    c("price", "numeric(10,2)"),
                    c("stock", "integer", default="0"),
                ],
                checks=["price >= 0", "stock >= 0"],
            ),
            Table(
                "orders",
                [
                    uuid_id(),
                    ref("customer_id", "customers.id", on_delete="RESTRICT"),
                    c("status", "order_status", default="'pending'"),
                    c("total", "numeric(12,2)"),
                    created(),
                ],
            ),
            Table(
                "order_items",
                [
                    uuid_id(),
                    ref("order_id", "orders.id"),
                    ref("product_id", "products.id", on_delete="RESTRICT"),
                    c("quantity", "integer"),
                    c("unit_price", "numeric(10,2)"),
                ],
                unique=[("order_id", "product_id")],
                checks=["quantity > 0"],
            ),
        ],
        enums={"order_status": ["pending", "paid", "shipped", "cancelled"]},
    )


def school() -> Design:
    return Design(
        [
            Table(
                "students",
                [
                    uuid_id(),
                    c("full_name", "text"),
                    c("email", "varchar(255)", "unique"),
                    c("enrolled_on", "date"),
                ],
            ),
            Table(
                "teachers",
                [uuid_id(), c("full_name", "text"), c("email", "varchar(255)", "unique")],
            ),
            Table(
                "courses",
                [
                    uuid_id(),
                    c("code", "varchar(20)", "unique"),
                    c("title", "text"),
                    c("credits", "integer"),
                    ref("teacher_id", "teachers.id", "null", on_delete="SET NULL"),
                ],
                checks=["credits > 0"],
            ),
            Table(
                "enrollments",
                [
                    uuid_id(),
                    ref("student_id", "students.id"),
                    ref("course_id", "courses.id"),
                    c("grade", "integer", "null"),
                ],
                unique=[("student_id", "course_id")],
                checks=["grade BETWEEN 0 AND 100"],
            ),
        ]
    )


def booking() -> Design:
    return Design(
        [
            Table(
                "rooms",
                [
                    uuid_id(),
                    c("name", "varchar(100)", "unique"),
                    c("capacity", "integer"),
                    c("floor", "integer", "null"),
                ],
                checks=["capacity > 0"],
            ),
            Table(
                "guests",
                [
                    uuid_id(),
                    c("full_name", "text"),
                    c("email", "varchar(255)", "unique"),
                    c("phone", "varchar(30)", "null"),
                ],
            ),
            Table(
                "bookings",
                [
                    uuid_id(),
                    ref("room_id", "rooms.id", on_delete="RESTRICT"),
                    ref("guest_id", "guests.id", on_delete="RESTRICT"),
                    c("starts_at", "timestamptz"),
                    c("ends_at", "timestamptz"),
                    c("status", "booking_status", default="'confirmed'"),
                    created(),
                ],
                checks=["ends_at > starts_at"],
            ),
        ],
        enums={"booking_status": ["confirmed", "cancelled"]},
    )


def saas() -> Design:
    return Design(
        [
            Table(
                "organizations",
                [uuid_id(), c("slug", "varchar(63)", "unique"), c("name", "text"), created()],
            ),
            Table(
                "users",
                [
                    uuid_id(),
                    c("email", "varchar(255)", "unique"),
                    c("name", "text", "null"),
                    created(),
                ],
            ),
            Table(
                "memberships",
                [
                    uuid_id(),
                    ref("organization_id", "organizations.id"),
                    ref("user_id", "users.id"),
                    c("role", "member_role", default="'member'"),
                ],
                unique=[("organization_id", "user_id")],
            ),
            Table(
                "projects",
                [
                    uuid_id(),
                    ref("organization_id", "organizations.id"),
                    c("name", "text"),
                    c("archived_at", "timestamptz", "null"),
                    created(),
                ],
                unique=[("organization_id", "name")],
            ),
            Table(
                "audit_log",
                [
                    c("id", "bigint", "identity"),
                    ref("organization_id", "organizations.id"),
                    ref("actor_id", "users.id", "null", on_delete="SET NULL"),
                    c("action", "text"),
                    c("details", "jsonb", default="'{}'::jsonb"),
                    created(),
                ],
            ),
        ],
        enums={"member_role": ["owner", "admin", "member"]},
    )


DESIGNS = {"blog": blog, "shop": shop, "school": school, "booking": booking, "saas": saas}
REFERENCE_DDL = {name: ddl(build()) for name, build in DESIGNS.items()}
REFERENCE_MODELS = {name: models(build()) for name, build in DESIGNS.items()}

assert re.match(r"^[A-Z]", class_name("users"))  # import-time sanity: names are classes
