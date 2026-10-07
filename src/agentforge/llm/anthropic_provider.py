"""Anthropic (Claude) adapter using the official async SDK."""

from __future__ import annotations

from typing import Any

import anthropic

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
    "end_turn": StopReason.END_TURN,
    "tool_use": StopReason.TOOL_USE,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "refusal": StopReason.REFUSAL,
}


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
        retry: RetryPolicy | None = None,
        pricing: PriceTable | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        super().__init__(model=model, timeout_s=timeout_s, retry=retry, pricing=pricing)
        if client is None:
            if not api_key:
                raise LLMConfigError("ANTHROPIC_API_KEY is not set", provider=self.name)
            # SDK retries are disabled: LLMProvider owns retries so they are logged and testable.
            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
        self._client = client

    async def _complete(self, request: LLMRequest, model: str) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": request.max_tokens,
            "messages": to_anthropic_messages(request.messages),
        }
        if request.system_prompt:
            kwargs["system"] = request.system_prompt
        if request.tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
        try:
            raw = await self._client.messages.create(**kwargs)
        except anthropic.APIError as exc:
            raise _map_error(exc) from exc
        return _from_anthropic(raw, provider=self.name)


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert our messages to Anthropic's format.

    System messages are sent separately. Tool results become ``tool_result``
    blocks in a user turn, and consecutive turns with the same role are merged,
    because Anthropic expects user and assistant turns to alternate.
    """
    turns: list[dict[str, Any]] = []
    for message in messages:
        if message.role is Role.SYSTEM:
            continue
        role, blocks = _to_blocks(message)
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})
    return turns


def _to_blocks(message: Message) -> tuple[str, list[dict[str, Any]]]:
    if message.role is Role.TOOL:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id,
            "content": message.content,
        }
        if message.is_error:
            block["is_error"] = True
        return "user", [block]
    blocks: list[dict[str, Any]] = []
    if message.content:
        blocks.append({"type": "text", "text": message.content})
    for call in message.tool_calls:
        blocks.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return message.role.value, blocks


def _from_anthropic(raw: anthropic.types.Message, *, provider: str) -> LLMResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in raw.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            if not isinstance(block.input, dict):
                raise LLMResponseError("tool_use input is not an object", provider=provider)
            calls.append(
                ToolCall.model_validate(
                    {"id": block.id, "name": block.name, "arguments": block.input}
                )
            )
    return LLMResponse(
        message=Message.assistant("".join(texts), tool_calls=calls),
        stop_reason=_STOP_REASONS.get(raw.stop_reason or "", StopReason.OTHER),
        usage=Usage(input_tokens=raw.usage.input_tokens, output_tokens=raw.usage.output_tokens),
        model=raw.model,
        provider=provider,
    )


def _map_error(exc: anthropic.APIError) -> LLMError:
    provider = AnthropicProvider.name
    if isinstance(exc, anthropic.APITimeoutError):
        return LLMTimeoutError("request timed out", provider=provider)
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMConnectionError(f"connection failed: {exc.message}", provider=provider)
    if isinstance(exc, anthropic.APIStatusError):
        retry_after = parse_retry_after(exc.response.headers.get("retry-after"))
        return error_for_status(
            exc.status_code, exc.message, provider=provider, retry_after=retry_after
        )
    return LLMResponseError(f"unexpected response: {exc.message}", provider=provider)
