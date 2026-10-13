"""Load SQLAlchemy 2.0 declarative models in a separate interpreter and describe their tables.

The model source is written by an LLM, so it never runs in this process. It runs in
``python -I`` in a temporary directory with a minimal environment and a timeout,
and only a JSON description (tables, columns, keys) comes back. The description is
what :func:`differences` compares with the DDL.

The source must define a ``DeclarativeBase`` subclass called ``Base``.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from agentforge.tools.builtin.process import run_process
from agentforge.tools.pytest_runner import safe_env
from agentforge.tools.sql_schema import Schema, base_type

TIMEOUT_S = 30.0

_RUNNER = """
import json, sys
sys.path.insert(0, sys.argv[1])
import models
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

base = getattr(models, "Base", None)
if base is None or not hasattr(base, "metadata"):
    raise SystemExit("the module must define a declarative base class named Base")
configure_mappers()
out = {}
for table in base.metadata.sorted_tables:
    CreateTable(table).compile(dialect=postgresql.dialect())
    out[table.name] = {
        "columns": {c.name: bool(c.nullable) for c in table.columns},
        "primary_key": [c.name for c in table.primary_key.columns],
        "foreign_keys": sorted(
            [[c.parent.name, c.column.table.name, c.column.name] for c in table.foreign_keys]
        ),
    }
print(json.dumps(out))
"""


@dataclass
class ModelTable:
    columns: dict[str, bool] = field(default_factory=dict)  # name -> nullable
    primary_key: tuple[str, ...] = ()
    foreign_keys: list[tuple[str, str, str]] = field(default_factory=list)  # column, table, column


class ModelsError(ValueError):
    """The models do not import, configure or compile; the message is the last error line."""


async def introspect(source: str) -> dict[str, ModelTable]:
    """Describe the tables of ``source``'s ``Base.metadata``, or raise :class:`ModelsError`."""
    with tempfile.TemporaryDirectory(prefix="agentforge-models-") as tmp:
        root = Path(tmp)
        (root / "models.py").write_text(source, encoding="utf-8")
        (root / "_run.py").write_text(_RUNNER, encoding="utf-8")
        result = await run_process(
            [sys.executable, "-I", str(root / "_run.py"), str(root)],
            cwd=tmp,
            timeout_s=TIMEOUT_S,
            env=safe_env(),
            inherit_env=False,
        )
    if result.timed_out:
        raise ModelsError(f"importing the models took longer than {TIMEOUT_S:.0f}s")
    if result.exit_code != 0:
        lines = (result.stderr or result.stdout).strip().splitlines()
        raise ModelsError(lines[-1] if lines else f"exit code {result.exit_code}")
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ModelsError("the models produced no table description") from exc
    return {
        name: ModelTable(
            columns=info["columns"],
            primary_key=tuple(info["primary_key"]),
            foreign_keys=[tuple(fk) for fk in info["foreign_keys"]],
        )
        for name, info in data.items()
    }


def differences(schema: Schema, models: dict[str, ModelTable]) -> list[str]:
    """Where the models and the DDL disagree: tables, columns, nullability, keys."""
    found: list[str] = []
    for name in sorted(schema.tables.keys() - models.keys()):
        found.append(f"table {name} is in the DDL but has no model")
    for name in sorted(models.keys() - schema.tables.keys()):
        found.append(f"model for table {name} has no CREATE TABLE")
    for name in sorted(schema.tables.keys() & models.keys()):
        table, model = schema.tables[name], models[name]
        missing = [c for c in table.columns if c not in model.columns]
        extra = [c for c in model.columns if c not in table.columns]
        if missing:
            found.append(f"model {name} lacks column(s): {', '.join(missing)}")
        if extra:
            found.append(f"model {name} has column(s) not in the DDL: {', '.join(extra)}")
        for column, nullable in model.columns.items():
            ddl = table.columns.get(column)
            if ddl and nullable == ddl.not_null and base_type(ddl.type) not in {"serial"}:
                found.append(
                    f"{name}.{column} is {'NOT NULL' if ddl.not_null else 'nullable'} in the "
                    f"DDL but {'nullable' if nullable else 'not nullable'} in the model"
                )
        if tuple(sorted(model.primary_key)) != tuple(sorted(table.primary_key)):
            found.append(
                f"{name} primary key is ({', '.join(table.primary_key)}) in the DDL but "
                f"({', '.join(model.primary_key)}) in the model"
            )
        ddl_fks = sorted(
            (c, fk.ref_table, r)
            for fk in table.foreign_keys
            for c, r in zip(fk.columns, fk.ref_columns, strict=False)
        )
        if ddl_fks != sorted(model.foreign_keys):
            found.append(
                f"{name} foreign keys differ: DDL has {_fks(ddl_fks)}, model has "
                f"{_fks(sorted(model.foreign_keys))}"
            )
    return found


def _fks(items: list[tuple[str, str, str]]) -> str:
    return ", ".join(f"{c}->{t}.{r}" for c, t, r in items) or "none"
