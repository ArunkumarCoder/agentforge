"""The provider-neutral LLM interface.

Agents talk to ``LLMProvider.complete(LLMRequest) -> LLMResponse`` and never
import a vendor SDK. Each adapter only implements ``_complete`` (one attempt,
SDK errors mapped to ``agentforge.llm.errors``); this base class adds:

- a hard per-attempt timeout (safety net on top of the SDK's own timeout),
- cost calculation from token usage (see ``pricing.py``),
- retries with exponential backoff for retryable errors,
- structured logs for every request, retry and failure.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import Field, model_validator

from agentforge.core.schemas import Contract, Message, Role, Usage
from agentforge.llm.errors import LLMConfigError, LLMError, LLMTimeoutError
from agentforge.llm.pricing import PriceTable
from agentforge.llm.retry import RetryPolicy, with_retries
from agentforge.log import get_logger


class ToolSpec(Contract):
    """A tool the model may call, described by a JSON Schema for its arguments."""

    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class LLMRequest(Contract):
    """One call to a model: the conversation so far plus the tools it may use."""

    messages: list[Message] = Field(min_length=1)
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int = Field(default=1024, gt=0)
    model: str | None = None  # overrides the provider's default model

    @model_validator(mode="after")
    def _needs_a_non_system_message(self) -> LLMRequest:
        if all(m.role is Role.SYSTEM for m in self.messages):
            raise ValueError("a request needs at least one non-system message")
        return self

    @property
    def system_prompt(self) -> str:
        """All system messages joined, for providers that take ``system`` separately."""
        return "\n\n".join(m.content for m in self.messages if m.role is Role.SYSTEM)


class StopReason(StrEnum):
    END_TURN = "end_turn"  # the model finished its answer
    TOOL_USE = "tool_use"  # the model wants to call one or more tools
    MAX_TOKENS = "max_tokens"  # the answer was cut off by max_tokens
    STOP_SEQUENCE = "stop_sequence"
    REFUSAL = "refusal"  # declined or blocked by a content filter
    OTHER = "other"


class LLMResponse(Contract):
    """The model's reply, normalised across providers."""

    message: Message
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)
    model: str
    provider: str
    latency_ms: float = Field(default=0.0, ge=0)
    cached: bool = False  # True when served from the response cache (no tokens spent)

    @property
    def text(self) -> str:
        return self.message.content

    @property
    def wants_tools(self) -> bool:
        return bool(self.message.tool_calls)


class LLMProvider(ABC):
    """Base class for LLM adapters (Anthropic, OpenAI, Ollama, Fake, ...)."""

    name: ClassVar[str]
    is_local: ClassVar[bool] = False  # local models (Ollama, Fake) cost nothing

    def __init__(
        self,
        *,
        model: str,
        timeout_s: float = 60.0,
        retry: RetryPolicy | None = None,
        pricing: PriceTable | None = None,
    ) -> None:
        if not model:
            raise LLMConfigError("a model name is required", provider=self.name)
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self.model = model
        self.timeout_s = timeout_s
        self.retry = retry or RetryPolicy()
        self.pricing = pricing or PriceTable.default()
        self._log = get_logger(f"llm.{self.name}")

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send ``request`` and return the normalised response, retrying when sensible."""
        model = request.model or self.model
        log = self._log.bind(provider=self.name, model=model)
        log.debug("llm.request", messages=len(request.messages), tools=len(request.tools))
        start = time.perf_counter()

        async def attempt() -> LLMResponse:
            try:
                async with asyncio.timeout(self.timeout_s):
                    return await self._complete(request, model)
            except TimeoutError as exc:
                raise LLMTimeoutError(
                    f"no response within {self.timeout_s}s", provider=self.name
                ) from exc

        try:
            response = await with_retries(attempt, self.retry, log=log)
        except LLMError as error:
            log.error(
                "llm.failed",
                error_type=type(error).__name__,
                status_code=error.status_code,
                error_message=str(error),
            )
            raise
        latency_ms = round((time.perf_counter() - start) * 1000, 3)
        cost = self.pricing.cost(response.model or model, response.usage, free=self.is_local)
        response = response.model_copy(
            update={
                "latency_ms": latency_ms,
                "usage": response.usage.model_copy(update={"cost_usd": cost}),
            }
        )
        log.info(
            "llm.response",
            stop_reason=response.stop_reason.value,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
        )
        return response

    @abstractmethod
    async def _complete(self, request: LLMRequest, model: str) -> LLMResponse:
        """Make exactly one API call. Must raise only ``LLMError`` subclasses."""
