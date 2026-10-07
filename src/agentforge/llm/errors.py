"""Provider-neutral LLM errors.

Every adapter translates its SDK's exceptions into these, so agents and the
retry logic never depend on a specific vendor's exception classes.
"""

from __future__ import annotations

from agentforge.core.errors import AgentForgeError


class LLMError(AgentForgeError):
    """Base class for LLM failures."""

    retryable: bool = False

    def __init__(self, message: str, *, provider: str = "", status_code: int | None = None):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class LLMConfigError(LLMError):
    """The provider cannot be created (missing API key, unknown provider, ...)."""


class LLMAuthError(LLMError):
    """The API key is missing, invalid or lacks permission (401/403)."""


class LLMBadRequestError(LLMError):
    """The request was rejected (400/404/413/422): fix the request, don't retry."""


class LLMResponseError(LLMError):
    """The provider answered, but the response could not be understood."""


class LLMRateLimitError(LLMError):
    """Too many requests (429). ``retry_after`` is the server's hint in seconds."""

    retryable = True

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        status_code: int | None = 429,
        retry_after: float | None = None,
    ):
        super().__init__(message, provider=provider, status_code=status_code)
        self.retry_after = retry_after


class LLMServerError(LLMError):
    """The provider failed or is overloaded (5xx, 529)."""

    retryable = True


class LLMConnectionError(LLMError):
    """The provider could not be reached."""

    retryable = True


class LLMTimeoutError(LLMError):
    """The request took longer than the configured timeout."""

    retryable = True


def error_for_status(
    status_code: int, message: str, *, provider: str, retry_after: float | None = None
) -> LLMError:
    """Map an HTTP status code from any provider to the matching LLMError."""
    if status_code == 429:
        return LLMRateLimitError(message, provider=provider, retry_after=retry_after)
    if status_code in (401, 403):
        return LLMAuthError(message, provider=provider, status_code=status_code)
    if status_code == 408:
        return LLMTimeoutError(message, provider=provider, status_code=status_code)
    if status_code >= 500:
        return LLMServerError(message, provider=provider, status_code=status_code)
    return LLMBadRequestError(message, provider=provider, status_code=status_code)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header given in seconds; ignore anything else."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None
