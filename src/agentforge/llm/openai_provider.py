"""OpenAI adapter (Chat Completions) using the official async SDK.

``base_url`` makes it usable with any OpenAI-compatible server later
(for example Ollama's ``/v1`` endpoint).
"""

from __future__ import annotations

import json
from typing import Any

import openai
from openai.types.chat import ChatCompletion
from pydantic import JsonValue

from agentforge.core.schemas import Message, Role, ToolCall, Usage
from agentforge.llm.base import LLMProvider, LLMRequest, LLMResponse, StopReason
from agentforge.llm.errors import (
    LLMConfigError,
    LLMConnectionError,
    LLMError,
    LLMResponseError,
    LLMTimeoutError,
    error_for_status,
    parse_retry_after,
)
from agentforge.llm.pricing import PriceTable
from agentforge.llm.retry import RetryPolicy

_STOP_REASONS = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.REFUSAL,
}


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 60.0,
        retry: RetryPolicy | None = None,
        pricing: PriceTable | None = None,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        super().__init__(model=model, timeout_s=timeout_s, retry=retry, pricing=pricing)
        if client is None:
            if not api_key:
                raise LLMConfigError("OPENAI_API_KEY is not set", provider=self.name)
            # SDK retries are disabled: LLMProvider owns retries so they are logged and testable.
            client = openai.AsyncOpenAI(
                api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0
            )
        self._client = client

    async def _complete(self, request: LLMRequest, model: str) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_completion_tokens": request.max_tokens,
            "messages": [to_openai_message(m) for m in request.messages],
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        try:
            raw = await self._client.chat.completions.create(**kwargs)
        except openai.APIError as exc:
            raise self._map_error(exc) from exc
        return _from_openai(raw, provider=self.name)

    def _map_error(self, exc: openai.APIError) -> LLMError:
        return map_openai_error(exc, self.name)


def to_openai_message(message: Message) -> dict[str, Any]:
    """Convert one of our messages to an OpenAI chat message."""
    if message.role is Role.TOOL:
        return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
    out: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.tool_calls:
        out["content"] = message.content or None
        out["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in message.tool_calls
        ]
    return out


def _from_openai(raw: ChatCompletion, *, provider: str) -> LLMResponse:
    if not raw.choices:
        raise LLMResponseError("response has no choices", provider=provider)
    choice = raw.choices[0]
    calls: list[ToolCall] = []
    for call in choice.message.tool_calls or []:
        if call.type != "function":
            continue
        calls.append(
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=_parse_arguments(call.function.arguments, provider),
            )
        )
    stop = _STOP_REASONS.get(choice.finish_reason, StopReason.OTHER)
    if choice.message.refusal:
        stop = StopReason.REFUSAL
    usage = Usage()
    if raw.usage is not None:
        usage = Usage(
            input_tokens=raw.usage.prompt_tokens, output_tokens=raw.usage.completion_tokens
        )
    return LLMResponse(
        message=Message.assistant(
            choice.message.content or choice.message.refusal or "", tool_calls=calls
        ),
        stop_reason=stop,
        usage=usage,
        model=raw.model,
        provider=provider,
    )


def _parse_arguments(arguments: str, provider: str) -> dict[str, JsonValue]:
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError as exc:
        raise LLMResponseError(
            f"tool arguments are not valid JSON: {exc}", provider=provider
        ) from exc
    if not isinstance(parsed, dict):
        raise LLMResponseError("tool arguments must be a JSON object", provider=provider)
    return parsed


def map_openai_error(exc: openai.APIError, provider: str) -> LLMError:
    """Translate an OpenAI SDK exception (also used by OpenAI-compatible servers)."""
    if isinstance(exc, openai.APITimeoutError):
        return LLMTimeoutError("request timed out", provider=provider)
    if isinstance(exc, openai.APIConnectionError):
        return LLMConnectionError(f"connection failed: {exc.message}", provider=provider)
    if isinstance(exc, openai.APIStatusError):
        retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
        return error_for_status(
            exc.status_code, exc.message, provider=provider, retry_after=retry_after
        )
    return LLMResponseError(f"unexpected response: {exc.message}", provider=provider)
