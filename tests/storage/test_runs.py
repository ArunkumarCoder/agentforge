"""The run store, on SQLite (always) and on a real PostgreSQL (when a scratch URL is given)."""

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from agentforge.agents.echo import EchoAgent
from agentforge.config import Settings, StorageSettings
from agentforge.core.schemas import RunResult, RunStatus
from agentforge.storage import RunStore, StorageError, open_store
from agentforge.storage.runs import database_url, metadata

REAL_URL = os.environ.get("AGENTFORGE_CHECK_STORAGE_URL")


def run_echo(data: dict[str, Any]) -> RunResult[Any]:
    return asyncio.run(EchoAgent().arun(data))


def ok(text_: str = "hi") -> RunResult[Any]:
    return run_echo({"text": text_})


def bad() -> RunResult[Any]:
    return run_echo({"text": ""})  # input validation failure


def aged(result: RunResult[Any], **ago: float) -> RunResult[Any]:
    return result.model_copy(update={"started_at": datetime.now(UTC) - timedelta(**ago)})


@pytest.fixture
def store() -> Iterator[RunStore]:
    s = RunStore("sqlite://")
    yield s
    s.close()


# --------------------------------------------------------------------------- saving and reading


def test_a_succeeded_run_round_trips(store: RunStore) -> None:
    result = ok("hello")
    store.save(result, input={"text": "hello"}, source="api")
    record = store.get(result.run_id)
    assert record is not None
    assert record.status is RunStatus.SUCCEEDED
    assert record.agent == "echo"
    assert record.output == {"text": "hello", "length": 5}
    assert record.input == {"text": "hello"}
    assert record.source == "api"
    assert record.error_kind is None
    assert record.correlation_id == result.correlation_id
    assert record.started_at == result.started_at
    assert record.started_at.tzinfo is not None
    assert record.duration_ms == pytest.approx(result.duration_ms)


def test_a_failed_run_keeps_its_error(store: RunStore) -> None:
    result = bad()
    store.save(result, input={"text": ""})
    record = store.get(result.run_id)
    assert record is not None
    assert record.status is RunStatus.FAILED
    assert record.error_kind == "input_validation"
    assert record.error_type
    assert record.error_message
    assert record.output is None


def test_usage_is_saved(store: RunStore) -> None:
    result = ok().model_copy(
        update={
            "usage": ok().usage.model_copy(
                update={"input_tokens": 7, "output_tokens": 3, "cost_usd": 0.25}
            )
        }
    )
    store.save(result)
    record = store.get(result.run_id)
    assert record is not None
    assert (record.input_tokens, record.output_tokens, record.cost_usd) == (7, 3, 0.25)


def test_unknown_run_is_none(store: RunStore) -> None:
    assert store.get("nope") is None


def test_saving_twice_is_an_error(store: RunStore) -> None:
    result = ok()
    store.save(result)
    with pytest.raises(StorageError, match="already saved"):
        store.save(result)


def test_inputs_can_be_left_out() -> None:
    private = RunStore("sqlite://", store_inputs=False)
    result = ok("secret code")
    private.save(result, input={"text": "secret code"})
    record = private.get(result.run_id)
    assert record is not None
    assert record.input is None
    assert record.output == {"text": "secret code", "length": 11}


def test_an_input_that_is_not_json_is_a_storage_error_not_a_crash(store: RunStore) -> None:
    with pytest.raises(StorageError, match="cannot save"):
        store.save(ok(), input={"x": object()})


def test_try_save_never_raises(store: RunStore, logs: Any) -> None:
    result = ok()
    assert store.try_save(result) is True
    assert store.try_save(result) is False  # duplicate
    assert "runs.save_failed" in logs.events()
    metadata.drop_all(store.engine)  # the database is broken under it
    assert store.try_save(ok()) is False
    assert logs.events().count("runs.save_failed") == 2


# --------------------------------------------------------------------------- listing


def seed(store: RunStore) -> list[RunResult[Any]]:
    runs = [
        aged(ok("a"), hours=5),
        aged(bad(), hours=4),
        aged(ok("b"), hours=3),
        aged(ok("c"), hours=2),
        aged(bad(), hours=1),
    ]
    for r in runs:
        store.save(r)
    return runs


def test_list_is_newest_first_and_paged(store: RunStore) -> None:
    runs = seed(store)
    newest_first = [r.run_id for r in reversed(runs)]
    page = store.list_runs(limit=2)
    assert page.total == 5
    assert [r.run_id for r in page.runs] == newest_first[:2]
    second = store.list_runs(limit=2, offset=2)
    assert [r.run_id for r in second.runs] == newest_first[2:4]
    assert [r.run_id for r in store.list_runs(limit=2, offset=4).runs] == newest_first[4:]


def test_list_filters_by_status_and_agent(store: RunStore) -> None:
    seed(store)
    failed = store.list_runs(status=RunStatus.FAILED)
    assert failed.total == 2
    assert {r.error_kind for r in failed.runs} == {"input_validation"}
    assert store.list_runs(agent="echo").total == 5
    assert store.list_runs(agent="other").total == 0
    assert store.list_runs(agent="echo", status=RunStatus.SUCCEEDED).total == 3


def test_list_does_not_return_inputs_or_outputs(store: RunStore) -> None:
    seed(store)
    assert not hasattr(store.list_runs().runs[0], "output")


# --------------------------------------------------------------------------- stats and pruning


def test_stats_per_agent(store: RunStore) -> None:
    seed(store)
    other = run_echo({"text": "z"}).model_copy(update={"agent": "other"})
    store.save(other)
    stats = {s.agent: s for s in store.stats()}
    echo = stats["echo"]
    assert (echo.runs, echo.succeeded, echo.failed) == (5, 3, 2)
    assert echo.success_rate == pytest.approx(0.6)
    assert stats["other"].runs == 1
    assert next(s.agent for s in store.stats()) == "echo"  # busiest first
    assert [s.agent for s in store.stats(agent="other")] == ["other"]


def test_stats_since(store: RunStore) -> None:
    seed(store)
    recent = store.stats(since=timedelta(minutes=150))  # the last two runs
    assert recent[0].runs == 2
    assert store.stats(since=timedelta(minutes=1)) == []


def test_stats_sums_tokens_and_cost(store: RunStore) -> None:
    base = ok()
    for i in range(2):
        usage = base.usage.model_copy(
            update={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5}
        )
        store.save(run_echo({"text": str(i)}).model_copy(update={"usage": usage}))
    (stat,) = store.stats()
    assert (stat.input_tokens, stat.output_tokens, stat.cost_usd) == (20, 10, 1.0)


def test_prune_deletes_only_old_runs(store: RunStore) -> None:
    runs = seed(store)
    deleted = store.prune(timedelta(hours=2, minutes=30))  # the 5h, 4h and 3h runs
    assert deleted == 3
    assert {r.run_id for r in store.list_runs().runs} == {runs[3].run_id, runs[4].run_id}
    assert store.prune(timedelta(days=1)) == 0


# --------------------------------------------------------------------------- opening


def test_a_file_database_survives_reopening(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'deep' / 'er' / 'runs.db'}"  # parent folders are created
    first = RunStore(url)
    result = ok()
    first.save(result)
    first.close()
    second = RunStore(url)
    assert second.get(result.run_id) is not None
    assert second.backend == "sqlite"
    second.close()


def test_postgres_urls_use_the_bundled_driver() -> None:
    for scheme in ("postgresql", "postgres"):
        assert database_url(f"{scheme}://u:p@h/db").drivername == "postgresql+psycopg"
    assert database_url("postgresql+psycopg://u:p@h/db").drivername == "postgresql+psycopg"


def test_unsupported_and_malformed_urls_are_rejected() -> None:
    with pytest.raises(StorageError, match="unsupported database 'mysql'"):
        database_url("mysql://u:p@h/db")
    with pytest.raises(StorageError, match="not a database URL"):
        database_url("not a url")


def test_an_unreachable_database_is_a_storage_error_without_the_password() -> None:
    with pytest.raises(StorageError) as caught:
        RunStore("postgresql://user:hunter2@127.0.0.1:1/none")
    assert "cannot open the run store" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_open_store_follows_the_settings(tmp_path: Path) -> None:
    assert open_store(Settings(_env_file=None)) is None  # type: ignore[call-arg]
    blank = Settings(_env_file=None, storage=StorageSettings(url="  "))  # type: ignore[call-arg]
    assert open_store(blank) is None
    configured = Settings(  # type: ignore[call-arg]
        _env_file=None,
        storage=StorageSettings(url=f"sqlite:///{tmp_path / 'r.db'}", store_inputs=False),
    )
    opened = open_store(configured)
    assert opened is not None
    assert opened.store_inputs is False
    opened.close()


# --------------------------------------------------------------------------- real PostgreSQL


@pytest.mark.integration
@pytest.mark.skipif(
    REAL_URL is None, reason="set AGENTFORGE_CHECK_STORAGE_URL (a scratch database)"
)
def test_the_same_behaviour_on_a_real_postgres() -> None:
    assert REAL_URL is not None
    pg = RunStore(REAL_URL)
    try:
        assert pg.backend == "postgresql"
        with pg.engine.begin() as conn:
            conn.execute(text("DELETE FROM runs"))
        runs = seed(pg)
        good = runs[0]
        pg.save(ok("pg"), input={"text": "pg", "nested": {"a": [1, 2]}})
        assert pg.list_runs().total == 6
        assert pg.list_runs(status=RunStatus.FAILED).total == 2
        record = pg.get(good.run_id)
        assert record is not None
        assert record.started_at == good.started_at
        found = [r for r in pg.list_runs().runs if r.run_id != good.run_id]
        newest = pg.get(found[0].run_id)
        assert newest is not None
        assert newest.input == {"text": "pg", "nested": {"a": [1, 2]}}  # JSONB round trip
        (stat,) = pg.stats()
        assert (stat.runs, stat.failed) == (6, 2)
        assert pg.prune(timedelta(hours=2, minutes=30)) == 3
        with pytest.raises(StorageError, match="already saved"):
            pg.save(runs[4])
    finally:
        metadata.drop_all(pg.engine)
        pg.close()
