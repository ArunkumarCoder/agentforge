"""Helpers for LLM tests: a fake HTTP API and an instant retry policy.

Real SDK clients are used with an in-memory transport, so the tests cover the
actual request payload the SDK sends and the SDK's own response parsing,
without any network access.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from agentforge.llm import RetryPolicy

Reply = httpx2.Response | Exception


class MockAPI:
    """Queue of canned HTTP replies; records every request body it receives."""

    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.requests: list[dict[str, Any]] = []

    def reply(
        self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        self.replies.append(httpx2.Response(status, json=body, headers=headers))

    def fail(self, error: Exception) -> None:
        self.replies.append(error)

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(json.loads(request.content or b"{}"))
        if not self.replies:
            raise AssertionError("MockAPI received more requests than queued replies")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def http_client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]


class RecordingSleep:
    """Stands in for asyncio.sleep: records delays instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def api() -> MockAPI:
    return MockAPI()


@pytest.fixture
def sleep() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
def make_retry(sleep: RecordingSleep) -> Callable[..., RetryPolicy]:
    def factory(max_retries: int = 2, **kwargs: Any) -> RetryPolicy:
        return RetryPolicy(max_retries=max_retries, sleep=sleep, rng=random.Random(0), **kwargs)

    return factory
