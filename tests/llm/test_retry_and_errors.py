"""Tests for error mapping and the retry policy."""

import random
from collections.abc import Callable

import pytest

from agentforge.llm import (
    LLMAuthError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMError,
    LLMRateLimitError,
    LLMServerError,
    LLMTimeoutError,
    RetryPolicy,
)
from agentforge.llm.errors import error_for_status, parse_retry_after
from agentforge.llm.retry import with_retries
from agentforge.log import get_logger
from tests.conftest import LogCapture
from tests.llm.conftest import RecordingSleep


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [
        (400, LLMBadRequestError, False),
        (401, LLMAuthError, False),
        (403, LLMAuthError, False),
        (404, LLMBadRequestError, False),
        (408, LLMTimeoutError, True),
        (422, LLMBadRequestError, False),
        (429, LLMRateLimitError, True),
        (500, LLMServerError, True),
        (529, LLMServerError, True),  # Anthropic "overloaded"
    ],
)
def test_error_for_status(status: int, expected: type[LLMError], retryable: bool) -> None:
    error = error_for_status(status, "msg", provider="p")
    assert type(error) is expected
    assert error.retryable is retryable
    assert error.provider == "p"


def test_rate_limit_keeps_retry_after() -> None:
    error = error_for_status(429, "slow down", provider="p", retry_after=7)
    assert isinstance(error, LLMRateLimitError)
    assert error.retry_after == 7
    assert error.status_code == 429


@pytest.mark.parametrize(
    ("header", "expected"),
    [("3", 3.0), ("0.5", 0.5), (None, None), ("soon", None), ("-1", None)],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


class TestRetryPolicy:
    def test_backoff_is_exponential_with_full_jitter(self) -> None:
        policy = RetryPolicy(base_delay_s=1, max_delay_s=100, rng=random.Random(42))
        error = LLMServerError("x")
        for attempt, ceiling in [(1, 1), (2, 2), (3, 4), (4, 8)]:
            delays = [policy.delay_for(attempt, error) for _ in range(200)]
            assert all(0 <= d <= ceiling for d in delays)
            assert max(delays) > ceiling * 0.8  # jitter really uses the whole range

    def test_delay_is_capped(self) -> None:
        policy = RetryPolicy(base_delay_s=1, max_delay_s=5)
        assert policy.delay_for(20, LLMServerError("x")) <= 5

    def test_retry_after_is_honoured_and_capped(self) -> None:
        policy = RetryPolicy(max_delay_s=10)
        assert policy.delay_for(1, LLMRateLimitError("x", retry_after=3)) == 3
        assert policy.delay_for(1, LLMRateLimitError("x", retry_after=60)) == 10

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_retries": -1}, {"base_delay_s": -1}, {"base_delay_s": 5, "max_delay_s": 1}],
    )
    def test_invalid_policy(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError, match=r"must be|need"):
            RetryPolicy(**kwargs)  # type: ignore[arg-type]


class Flaky:
    """Fails with the given errors, then returns 'ok'."""

    def __init__(self, *errors: LLMError) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


async def test_retries_until_success(
    make_retry: Callable[..., RetryPolicy], sleep: RecordingSleep, logs: LogCapture
) -> None:
    call = Flaky(LLMServerError("500"), LLMConnectionError("down"))
    assert await with_retries(call, make_retry(max_retries=3), log=get_logger()) == "ok"
    assert call.calls == 3
    assert len(sleep.delays) == 2
    retries = [e for e in logs.entries if e["event"] == "llm.retry"]
    assert [r["attempt"] for r in retries] == [1, 2]
    assert retries[0]["error_type"] == "LLMServerError"


async def test_gives_up_after_max_retries(
    make_retry: Callable[..., RetryPolicy], sleep: RecordingSleep
) -> None:
    call = Flaky(*(LLMTimeoutError("slow") for _ in range(5)))
    with pytest.raises(LLMTimeoutError):
        await with_retries(call, make_retry(max_retries=2), log=get_logger())
    assert call.calls == 3  # 1 attempt + 2 retries
    assert len(sleep.delays) == 2


async def test_non_retryable_error_is_raised_immediately(
    make_retry: Callable[..., RetryPolicy], sleep: RecordingSleep
) -> None:
    call = Flaky(LLMAuthError("bad key"))
    with pytest.raises(LLMAuthError):
        await with_retries(call, make_retry(max_retries=5), log=get_logger())
    assert call.calls == 1
    assert sleep.delays == []


async def test_zero_retries(make_retry: Callable[..., RetryPolicy]) -> None:
    call = Flaky(LLMServerError("500"))
    with pytest.raises(LLMServerError):
        await with_retries(call, make_retry(max_retries=0), log=get_logger())
    assert call.calls == 1
