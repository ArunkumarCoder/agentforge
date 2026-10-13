"""Run DDL on a real PostgreSQL server, inside a transaction that is always rolled back.

This is the strongest check there is: the server itself accepts or rejects the
statements. It is optional, because it needs a reachable server and the ``psql``
client. Set ``AGENTFORGE_CHECK_DATABASE_URL`` (for example
``postgresql://user:pass@localhost:5432/scratch``) to turn it on; point it at a
scratch database, never at data you care about. Nothing is kept: the DDL runs in
a throw-away schema and the transaction ends with ``ROLLBACK``.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agentforge.tools.builtin.process import run_process
from agentforge.tools.pytest_runner import safe_env

URL_ENV = "AGENTFORGE_CHECK_DATABASE_URL"
TIMEOUT_S = 60.0
_SCHEMA = "agentforge_check"
_PREFIX_LINES = 3  # lines we add before the DDL


@dataclass(frozen=True)
class ExecResult:
    ok: bool
    error: str = ""


def configured_url() -> str | None:
    return os.environ.get(URL_ENV) or None


def available() -> bool:
    """A URL is configured and the ``psql`` client is installed."""
    return configured_url() is not None and shutil.which("psql") is not None


async def execute_ddl(sql: str, url: str | None = None) -> ExecResult:
    """Run ``sql`` in a rolled-back transaction. ``ok`` is False with the server's message."""
    url = url or configured_url()
    psql = shutil.which("psql")
    if url is None or psql is None:
        return ExecResult(False, "no database URL configured or psql not installed")
    script = (
        "BEGIN;\n"
        f"CREATE SCHEMA {_SCHEMA};\n"
        f"SET LOCAL search_path TO {_SCHEMA}, public;\n"
        f"{sql.rstrip()}\n"
        "ROLLBACK;\n"
    )
    with tempfile.TemporaryDirectory(prefix="agentforge-pg-") as tmp:
        path = Path(tmp) / "schema.sql"
        path.write_text(script, encoding="utf-8")
        result = await run_process(
            [psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-d", url, "-f", str(path)],
            cwd=tmp,
            timeout_s=TIMEOUT_S,
            env=safe_env(),
            inherit_env=False,
        )
    if result.timed_out:
        return ExecResult(False, f"the database took longer than {TIMEOUT_S:.0f}s")
    if result.exit_code == 0:
        return ExecResult(True)
    return ExecResult(False, _explain(result.stderr or result.stdout))


def _explain(stderr: str) -> str:
    """``psql:schema.sql:7: ERROR: ...`` -> ``line 4: ERROR: ...`` (numbered within the DDL)."""
    for line in stderr.splitlines():
        match = re.search(r"schema\.sql:(\d+): (ERROR:.*)", line)
        if match:
            number = max(int(match.group(1)) - _PREFIX_LINES, 1)
            return f"line {number} of your DDL: {match.group(2)[:300]}"
    return (
        stderr.strip().splitlines()[-1][:300] if stderr.strip() else "the server rejected the DDL"
    )
