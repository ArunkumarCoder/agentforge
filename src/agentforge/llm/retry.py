"""Retry with exponential backoff and full jitter.

Only errors marked ``retryable`` (rate limits, 5xx, timeouts, connection
errors) are retried. A server ``Retry-After`` hint is honoured, capped at
``max_delay_s``. ``sleep`` and ``rng`` are injectable so tests run instantly
and deterministically.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from agentforge.llm.errors import LLMError, LLMRateLimitError

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 30.0
    sleep: Sleep = field(default=asyncio.sleep, repr=False, compare=False)
    rng: random.Random = field(default_factory=random.Random, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if self.base_delay_s < 0 or self.max_delay_s < self.base_delay_s:
            raise ValueError("need 0 <= base_delay_s <= max_delay_s")

    def delay_for(self, attempt: int, error: LLMError) -> float:
        """Seconds to wait before retry number ``attempt`` (1-based)."""
        if isinstance(error, LLMRateLimitError) and error.retry_after is not None:
            return min(error.retry_after, self.max_delay_s)
        ceiling = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 1))
        return self.rng.uniform(0, ceiling)


async def with_retries[T](
    call: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    log: structlog.typing.FilteringBoundLogger,
) -> T:
    """Run ``call``, retrying retryable LLMErrors according to ``policy``."""
    attempt = 0
    while True:
        try:
            return await call()
        except LLMError as error:
            attempt += 1
            if not error.retryable or attempt > policy.max_retries:
                raise
            delay = policy.delay_for(attempt, error)
            log.warning(
                "llm.retry",
                attempt=attempt,
                max_retries=policy.max_retries,
                delay_s=round(delay, 3),
                error_type=type(error).__name__,
                status_code=error.status_code,
            )
            await policy.sleep(delay)
