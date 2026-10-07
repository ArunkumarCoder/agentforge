"""Shared pytest fixtures."""

import io
import json
import os
from typing import Any

import pytest

from agentforge.config import LogFormat, LoggingSettings, Settings
from agentforge.log import configure_logging

_PROVIDER_KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}


@pytest.fixture(autouse=True)
def _hermetic_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove AGENTFORGE_* and provider-key variables so tests behave the same everywhere.

    Without this, a developer's shell or CI settings (e.g. AGENTFORGE_ENV=test)
    would leak into tests that rely on defaults. Live tests (marked ``llm``)
    keep the environment because they need the real API keys.
    """
    if request.node.get_closest_marker("llm"):
        return
    for name in list(os.environ):
        if name.startswith("AGENTFORGE_") or name in _PROVIDER_KEYS:
            monkeypatch.delenv(name, raising=False)


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
