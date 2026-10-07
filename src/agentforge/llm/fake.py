"""FakeProvider: a scripted, offline LLM for tests, demos and CI.

Two modes:

- **Echo** (no script): answers ``"[fake] <last user message>"``. This is what
  ``AGENTFORGE_LLM__PROVIDER=fake`` gives you, so the app runs without keys.
- **Scripted**: each call returns the next item of the script. An item can be

  - a ``str`` → a plain text answer,
  - a ``ToolCall`` or ``list[ToolCall]`` → the model asks to call tools,
  - an ``LLMResponse`` → returned as-is,
  - an ``LLMError`` → raised (to test retries and failure handling),
  - a callable ``(LLMRequest) -> any of the above`` → computed per request.

Every request is recorded in ``provider.requests`` for assertions.

Example::

    llm = FakeProvider(script=[ToolCall(id="t1", name="read_file", arguments={"path": "a.py"}),
                               "a.py defines one function."])
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import ClassVar

from agentforge.core.schemas import Message, Role, ToolCall, Usage
from agentforge.llm.base import LLMProvider, LLMRequest, LLMResponse, StopReason
from agentforge.llm.errors import LLMError
from agentforge.llm.pricing import PriceTable
from agentforge.llm.retry import RetryPolicy

ScriptItem = (
    str
    | ToolCall
    | list[ToolCall]
    | LLMResponse
    | LLMError
    | Callable[[LLMRequest], "str | ToolCall | list[ToolCall] | LLMResponse"]
)


class FakeScriptExhaustedError(AssertionError):
    """The code under test made more LLM calls than the script provides."""


class FakeProvider(LLMProvider):
    name = "fake"
    is_local: ClassVar[bool] = True

    def __init__(
        self,
        *,
        model: str = "fake-model",
        script: Iterable[ScriptItem] | None = None,
        timeout_s: float = 60.0,
        retry: RetryPolicy | None = None,
        pricing: PriceTable | None = None,
    ) -> None:
        super().__init__(
            model=model,
            timeout_s=timeout_s,
            retry=retry or RetryPolicy(max_retries=0),
            pricing=pricing,
        )
        self._script: list[ScriptItem] | None = list(script) if script is not None else None
        self.requests: list[LLMRequest] = []

    @property
    def remaining(self) -> int:
        """Script items not yet used (0 in echo mode)."""
        return len(self._script) if self._script is not None else 0

    async def _complete(self, request: LLMRequest, model: str) -> LLMResponse:
        self.requests.append(request)
        if self._script is None:
            return self._build(request, f"[fake] {_last_user_text(request)}", model)
        if not self._script:
            raise FakeScriptExhaustedError(
                f"FakeProvider script exhausted after {len(self.requests) - 1} call(s)"
            )
        item = self._script.pop(0)
        if isinstance(item, LLMError):
            raise item
        if callable(item) and not isinstance(item, LLMResponse):
            item = item(request)
        if isinstance(item, LLMResponse):
            return item
        return self._build(request, item, model)

    def _build(
        self, request: LLMRequest, item: str | ToolCall | list[ToolCall], model: str
    ) -> LLMResponse:
        if isinstance(item, str):
            message = Message.assistant(item)
            stop = StopReason.END_TURN
        else:
            calls = [item] if isinstance(item, ToolCall) else item
            message = Message.assistant(tool_calls=calls)
            stop = StopReason.TOOL_USE
        return LLMResponse(
            message=message,
            stop_reason=stop,
            usage=Usage(input_tokens=_count_words(request), output_tokens=_words(message.content)),
            model=model,
            provider=self.name,
        )


def _words(text: str) -> int:
    return len(text.split())


def _count_words(request: LLMRequest) -> int:
    """Rough stand-in for token counting: one 'token' per word."""
    return sum(_words(m.content) for m in request.messages)


def _last_user_text(request: LLMRequest) -> str:
    for message in reversed(request.messages):
        if message.role is Role.USER:
            return message.content
    return ""
