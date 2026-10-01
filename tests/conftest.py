"""Shared pytest fixtures."""

import io
import json
from typing import Any

import pytest

from agentforge.config import LogFormat, LoggingSettings, Settings
from agentforge.log import configure_logging


class LogCapture:
    """Collects JSON log lines written during a test."""

    def __init__(self) -> None:
        self.stream = io.StringIO()

    @property
    def entries(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line]

    def events(self) -> list[str]:
        return [entry["event"] for entry in self.entries]


@pytest.fixture
def logs() -> LogCapture:
    """Route structured logs (DEBUG and up, JSON) into memory for assertions."""
    capture = LogCapture()
    settings = Settings(logging=LoggingSettings(level="DEBUG", format=LogFormat.JSON))
    configure_logging(settings, stream=capture.stream)
    return capture
