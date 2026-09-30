"""Structured logging with structlog.

- ``console`` format for local development, ``json`` for CI and production.
- Logs go to stderr so stdout stays free for CLI output.
- ``run_context()`` binds ``run_id`` and ``correlation_id`` to every log line
  emitted inside it, including from async tasks started within it.
- Secret-looking keys (api_key, token, password, ...) are redacted.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, TextIO

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from agentforge.config import LogFormat, Settings

REDACTED = "***"
_SENSITIVE_PARTS = ("api_key", "apikey", "token", "secret", "password", "authorization")


def new_id() -> str:
    """Return a short random identifier for runs and correlation IDs."""
    return uuid.uuid4().hex[:16]


def redact_secrets(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Replace values of secret-looking keys, at any nesting depth, with ``***``.

    Nested mappings are copied, so the caller's own dicts are never modified.
    """
    return _redacted(event_dict)


def _redacted(mapping: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in mapping.items():
        if any(part in str(key).lower() for part in _SENSITIVE_PARTS):
            clean[key] = REDACTED
        elif isinstance(value, Mapping):
            clean[key] = _redacted(value)
        else:
            clean[key] = value
    return clean


def _static_fields(settings: Settings) -> Processor:
    def add_fields(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", "agentforge")
        event_dict.setdefault("env", settings.env.value)
        return event_dict

    return add_fields


def configure_logging(settings: Settings, stream: TextIO | None = None) -> None:
    """Configure structlog. Safe to call more than once (e.g. in tests)."""
    renderer: Processor
    if settings.logging.format is LogFormat.JSON:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=stream is None and sys.stderr.isatty())

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _static_fields(settings),
            redact_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.logging.level]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=stream or sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str | None = None) -> structlog.typing.FilteringBoundLogger:
    """Return a logger; ``name`` is added as the ``logger`` field."""
    logger: structlog.typing.FilteringBoundLogger = structlog.get_logger()
    return logger.bind(logger=name) if name else logger


@contextmanager
def run_context(run_id: str | None = None, correlation_id: str | None = None) -> Iterator[str]:
    """Bind ``run_id`` / ``correlation_id`` to all logs inside the block; yields the run_id.

    A nested run keeps the outer ``correlation_id`` unless a new one is given,
    so every agent run inside one orchestration shares the same correlation_id.
    """
    current = structlog.contextvars.get_contextvars()
    rid = run_id or new_id()
    cid = correlation_id or current.get("correlation_id") or rid
    tokens = structlog.contextvars.bind_contextvars(run_id=rid, correlation_id=cid)
    try:
        yield rid
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
