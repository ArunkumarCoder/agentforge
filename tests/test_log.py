"""Tests for structured logging: fields, context binding and redaction."""

import asyncio
import io
import json
from typing import Any

import pytest

from agentforge.config import LogFormat, LoggingSettings, Settings
from agentforge.log import configure_logging, get_logger, is_sensitive_key, new_id, run_context


@pytest.fixture
def json_logs() -> io.StringIO:
    stream = io.StringIO()
    settings = Settings(logging=LoggingSettings(level="DEBUG", format=LogFormat.JSON))
    configure_logging(settings, stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line]


def test_json_log_has_standard_fields(json_logs: io.StringIO) -> None:
    get_logger("tests").info("hello", answer=42)
    (entry,) = lines(json_logs)
    assert entry["event"] == "hello"
    assert entry["answer"] == 42
    assert entry["level"] == "info"
    assert entry["logger"] == "tests"
    assert entry["service"] == "agentforge"
    assert entry["env"] == "dev"
    assert entry["timestamp"].endswith("Z")


def test_run_context_binds_and_unbinds_ids(json_logs: io.StringIO) -> None:
    log = get_logger()
    with run_context(run_id="run-1", correlation_id="corr-1") as rid:
        assert rid == "run-1"
        log.info("inside")
    log.info("outside")
    inside, outside = lines(json_logs)
    assert inside["run_id"] == "run-1"
    assert inside["correlation_id"] == "corr-1"
    assert "run_id" not in outside
    assert "correlation_id" not in outside


def test_nested_run_keeps_outer_correlation_id(json_logs: io.StringIO) -> None:
    log = get_logger()
    with run_context(correlation_id="orchestration-1"):
        with run_context(run_id="child") as child:
            log.info("child step")
        log.info("back in parent")
    child_entry, parent_entry = lines(json_logs)
    assert child == "child"
    assert child_entry["correlation_id"] == "orchestration-1"
    assert parent_entry["run_id"] != "child"  # outer run_id restored


def test_correlation_id_defaults_to_run_id(json_logs: io.StringIO) -> None:
    with run_context() as rid:
        get_logger().info("solo")
    (entry,) = lines(json_logs)
    assert entry["correlation_id"] == rid


def test_context_propagates_to_async_tasks(json_logs: io.StringIO) -> None:
    async def worker() -> None:
        get_logger().info("from task")

    async def main() -> None:
        with run_context(run_id="async-run"):
            await asyncio.gather(worker(), worker())

    asyncio.run(main())
    assert [e["run_id"] for e in lines(json_logs)] == ["async-run", "async-run"]


def test_secrets_are_redacted_at_any_depth(json_logs: io.StringIO) -> None:
    headers = {"Authorization": "Bearer xyz", "accept": "json"}
    get_logger().info(
        "calling provider", api_key="sk-live-abc", headers=headers, model="fake-model"
    )
    (entry,) = lines(json_logs)
    assert entry["api_key"] == "***"
    assert entry["headers"]["Authorization"] == "***"
    assert entry["headers"]["accept"] == "json"
    assert entry["model"] == "fake-model"
    assert "sk-live-abc" not in json_logs.getvalue()
    assert headers["Authorization"] == "Bearer xyz"  # caller's dict is not modified


def test_level_filtering() -> None:
    stream = io.StringIO()
    configure_logging(
        Settings(logging=LoggingSettings(level="WARNING", format=LogFormat.JSON)), stream=stream
    )
    log = get_logger()
    log.info("dropped")
    log.warning("kept")
    assert [e["event"] for e in lines(stream)] == ["kept"]


def test_console_format_is_human_readable() -> None:
    stream = io.StringIO()
    configure_logging(Settings(logging=LoggingSettings(format=LogFormat.CONSOLE)), stream=stream)
    get_logger().info("pretty", user="arun")
    output = stream.getvalue()
    assert "pretty" in output
    assert "user=arun" in output
    with pytest.raises(json.JSONDecodeError):
        json.loads(output)


def test_exceptions_are_rendered(json_logs: io.StringIO) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger().exception("failed")
    (entry,) = lines(json_logs)
    assert "ValueError: boom" in entry["exception"]


def test_new_id_is_unique_hex() -> None:
    ids = {new_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 16 and int(i, 16) >= 0 for i in ids)


@pytest.mark.parametrize(
    "key",
    [
        "api_key",
        "API-KEY",
        "x-api-key",
        "Authorization",
        "password",
        "access_token",
        "client_secret",
    ],
)
def test_sensitive_keys(key: str) -> None:
    assert is_sensitive_key(key)


@pytest.mark.parametrize(
    "key", ["input_tokens", "output_tokens", "total_tokens", "max_tokens", "model", "tokenizer"]
)
def test_token_counters_are_not_redacted(key: str) -> None:
    assert not is_sensitive_key(key)


def test_usage_fields_survive_redaction(json_logs: io.StringIO) -> None:
    get_logger().info("llm.response", input_tokens=12, output_tokens=7, access_token="abc")
    (entry,) = lines(json_logs)
    assert entry["input_tokens"] == 12
    assert entry["output_tokens"] == 7
    assert entry["access_token"] == "***"
