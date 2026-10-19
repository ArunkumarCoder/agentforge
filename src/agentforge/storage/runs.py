"""The run store: one row per agent run, in SQLite (default) or PostgreSQL.

Every run is saved, whether it succeeded or not, with its input, output, error, token
usage, cost and timing. That makes cost and failure rates something you can query
(``agentforge runs stats``) instead of something you remember.

Design notes:

- Plain SQLAlchemy Core, one table. ``JSON`` columns are ``JSONB`` on PostgreSQL.
- The schema is created on first use (``CREATE TABLE IF NOT EXISTS``). There are no
  migrations yet; the first schema change will bring Alembic.
- Saving never breaks a run: ``try_save`` logs and returns ``False`` on any error.
- Inputs can contain source code or other sensitive text. ``store_inputs=False`` keeps
  everything but the input, and ``prune`` deletes old rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import Field
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Engine,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    case,
    create_engine,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.pool import StaticPool

from agentforge.config import Settings
from agentforge.core.errors import AgentForgeError
from agentforge.core.schemas import Contract, RunResult, RunStatus

log = structlog.get_logger(__name__)

Source = Literal["cli", "api", "eval", "other"]

_JSON = JSON().with_variant(JSONB(), "postgresql")

metadata = MetaData()
runs = Table(
    "runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("correlation_id", String(64), nullable=False),
    Column("agent", String(100), nullable=False),
    Column("agent_version", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("source", String(16), nullable=False),
    Column("error_kind", String(32)),
    Column("error_type", String(200)),
    Column("error_message", Text),
    Column("input", _JSON),
    Column("output", _JSON),
    Column("input_tokens", Integer, nullable=False, default=0),
    Column("output_tokens", Integer, nullable=False, default=0),
    Column("cost_usd", Float, nullable=False, default=0.0),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Float, nullable=False),
    Index("ix_runs_agent_started", "agent", "started_at"),
    Index("ix_runs_started", "started_at"),
    Index("ix_runs_status", "status"),
)

_SUMMARY_COLUMNS = (
    runs.c.run_id,
    runs.c.agent,
    runs.c.agent_version,
    runs.c.status,
    runs.c.source,
    runs.c.error_kind,
    runs.c.input_tokens,
    runs.c.output_tokens,
    runs.c.cost_usd,
    runs.c.started_at,
    runs.c.duration_ms,
)


class StorageError(AgentForgeError):
    """The run store could not be opened or used."""


class RunSummary(Contract):
    """One run, without its (possibly large) input and output."""

    run_id: str
    agent: str
    agent_version: str
    status: RunStatus
    source: str
    error_kind: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    started_at: datetime
    duration_ms: float


class RunRecord(RunSummary):
    """One run in full."""

    correlation_id: str
    error_type: str | None = None
    error_message: str | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None


class RunPage(Contract):
    total: int
    limit: int
    offset: int
    runs: list[RunSummary]


class AgentStats(Contract):
    """Totals for one agent over some period."""

    agent: str
    runs: int
    succeeded: int
    failed: int
    success_rate: float = Field(ge=0, le=1)
    avg_duration_ms: float
    input_tokens: int
    output_tokens: int
    cost_usd: float


def database_url(url: str) -> URL:
    """Parse a URL, pointing plain ``postgresql://`` at the bundled psycopg driver."""
    try:
        parsed = make_url(url.strip())
    except Exception as exc:
        raise StorageError(f"not a database URL: {exc}") from None
    if parsed.drivername in {"postgresql", "postgres"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    if parsed.get_backend_name() not in {"sqlite", "postgresql"}:
        raise StorageError(
            f"unsupported database '{parsed.get_backend_name()}': use sqlite or postgresql"
        )
    return parsed


def _utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes; everything we store is UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RunStore:
    """Save and query agent runs."""

    def __init__(self, url: str, *, store_inputs: bool = True) -> None:
        parsed = database_url(url)
        self.store_inputs = store_inputs
        self.engine = self._engine(parsed)
        try:
            metadata.create_all(self.engine)
        except SQLAlchemyError as exc:
            self.engine.dispose()
            raise StorageError(f"cannot open the run store: {_reason(exc)}") from None

    @staticmethod
    def _engine(url: URL) -> Engine:
        if url.get_backend_name() == "sqlite":
            if url.database in (None, "", ":memory:"):
                return create_engine(
                    "sqlite://",
                    connect_args={"check_same_thread": False},
                    poolclass=StaticPool,
                )
            Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            return create_engine(url, connect_args={"timeout": 30})
        return create_engine(url, pool_pre_ping=True)

    def close(self) -> None:
        self.engine.dispose()

    @property
    def backend(self) -> str:
        return self.engine.url.get_backend_name()

    # ------------------------------------------------------------------ writing

    def save(
        self, result: RunResult[Any], *, input: dict[str, Any] | None = None, source: Source = "cli"
    ) -> None:
        """Insert one run. Raises ``StorageError`` if it cannot (or the id is already saved)."""
        error = result.error
        row = {
            "run_id": result.run_id,
            "correlation_id": result.correlation_id,
            "agent": result.agent,
            "agent_version": result.agent_version,
            "status": result.status.value,
            "source": source,
            "error_kind": error.kind.value if error else None,
            "error_type": error.type if error else None,
            "error_message": error.message if error else None,
            "input": input if self.store_inputs else None,
            "output": (
                result.output.model_dump(mode="json", serialize_as_any=True)
                if result.output is not None
                else None
            ),
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
            "cost_usd": result.usage.cost_usd,
            "started_at": _utc(result.started_at),
            "duration_ms": result.duration_ms,
        }
        try:
            with self.engine.begin() as conn:
                conn.execute(runs.insert().values(**row))
        except IntegrityError:
            raise StorageError(f"run {result.run_id} is already saved") from None
        except (SQLAlchemyError, TypeError, ValueError) as exc:
            raise StorageError(f"cannot save run {result.run_id}: {_reason(exc)}") from None

    def try_save(
        self, result: RunResult[Any], *, input: dict[str, Any] | None = None, source: Source = "cli"
    ) -> bool:
        """Like ``save``, but a failure is logged, never raised. A run must not fail
        because its history could not be written."""
        try:
            self.save(result, input=input, source=source)
        except Exception as exc:
            log.warning("runs.save_failed", run_id=result.run_id, error=str(exc))
            return False
        return True

    def prune(self, older_than: timedelta) -> int:
        """Delete runs that started more than ``older_than`` ago; returns how many."""
        cutoff = datetime.now(UTC) - older_than
        try:
            with self.engine.begin() as conn:
                result = conn.execute(delete(runs).where(runs.c.started_at < cutoff))
        except SQLAlchemyError as exc:
            raise StorageError(f"cannot prune runs: {_reason(exc)}") from None
        return int(result.rowcount)

    # ------------------------------------------------------------------ reading

    def get(self, run_id: str) -> RunRecord | None:
        try:
            with self.engine.connect() as conn:
                row = conn.execute(select(runs).where(runs.c.run_id == run_id)).mappings().first()
        except SQLAlchemyError as exc:
            raise StorageError(f"cannot read run {run_id}: {_reason(exc)}") from None
        if row is None:
            return None
        return RunRecord.model_validate({**row, "started_at": _utc(row["started_at"])})

    def list_runs(
        self,
        *,
        agent: str | None = None,
        status: RunStatus | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> RunPage:
        """Newest first."""
        conditions = _conditions(agent=agent, status=status)
        try:
            with self.engine.connect() as conn:
                total = conn.execute(select(func.count()).select_from(runs).where(*conditions))
                rows = (
                    conn.execute(
                        select(*_SUMMARY_COLUMNS)
                        .where(*conditions)
                        .order_by(runs.c.started_at.desc(), runs.c.run_id)
                        .limit(limit)
                        .offset(offset)
                    )
                    .mappings()
                    .all()
                )
                count = int(total.scalar_one())
        except SQLAlchemyError as exc:
            raise StorageError(f"cannot list runs: {_reason(exc)}") from None
        summaries = [
            RunSummary.model_validate({**r, "started_at": _utc(r["started_at"])}) for r in rows
        ]
        return RunPage(total=count, limit=limit, offset=offset, runs=summaries)

    def stats(
        self, *, agent: str | None = None, since: timedelta | None = None
    ) -> list[AgentStats]:
        """Per-agent totals, busiest first."""
        conditions = _conditions(agent=agent)
        if since is not None:
            conditions.append(runs.c.started_at >= datetime.now(UTC) - since)
        succeeded = func.sum(case((runs.c.status == RunStatus.SUCCEEDED.value, 1), else_=0))
        query = (
            select(
                runs.c.agent,
                func.count().label("runs"),
                succeeded.label("succeeded"),
                func.avg(runs.c.duration_ms).label("avg_duration_ms"),
                func.sum(runs.c.input_tokens).label("input_tokens"),
                func.sum(runs.c.output_tokens).label("output_tokens"),
                func.sum(runs.c.cost_usd).label("cost_usd"),
            )
            .where(*conditions)
            .group_by(runs.c.agent)
            .order_by(func.count().desc(), runs.c.agent)
        )
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(query).mappings().all()
        except SQLAlchemyError as exc:
            raise StorageError(f"cannot compute stats: {_reason(exc)}") from None
        stats: list[AgentStats] = []
        for r in rows:
            total, ok = int(r["runs"]), int(r["succeeded"] or 0)
            stats.append(
                AgentStats(
                    agent=r["agent"],
                    runs=total,
                    succeeded=ok,
                    failed=total - ok,
                    success_rate=ok / total,
                    avg_duration_ms=float(r["avg_duration_ms"] or 0.0),
                    input_tokens=int(r["input_tokens"] or 0),
                    output_tokens=int(r["output_tokens"] or 0),
                    cost_usd=float(r["cost_usd"] or 0.0),
                )
            )
        return stats


def _conditions(*, agent: str | None = None, status: RunStatus | None = None) -> list[Any]:
    conditions: list[Any] = []
    if agent is not None:
        conditions.append(runs.c.agent == agent)
    if status is not None:
        conditions.append(runs.c.status == status.value)
    return conditions


def _reason(exc: Exception) -> str:
    """A short, password-free reason (SQLAlchemy errors can quote the whole statement)."""
    text = str(getattr(exc, "orig", None) or exc).strip().splitlines()
    return text[0] if text else type(exc).__name__


def open_store(settings: Settings) -> RunStore | None:
    """The configured run store, or ``None`` when no URL is set."""
    url = settings.storage.url
    if url is None:
        return None
    return RunStore(url.get_secret_value(), store_inputs=settings.storage.store_inputs)
