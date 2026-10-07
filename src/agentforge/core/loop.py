"""The tool-calling loop: ask the model, run the tools it asks for, repeat.

    messages ──► LLM ──► answer? ──yes──► done
                  ▲        │ no (tool calls)
                  │        ▼
                  └── tool results ◄── run tools (in parallel)

Guards:

- ``max_steps`` caps the number of LLM calls. Hitting it raises
  ``ToolLoopLimitError``, which ``BaseAgent`` reports as a failed run.
- Tool failures never crash the loop: they go back to the model as error
  results so it can correct itself.
- Every LLM call's usage (tokens, cost) is added to the ``RunContext``.

Agents call ``run_tool_loop`` from ``execute``; the loop is not tied to any
particular agent.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from agentforge.core.agent import RunContext
from agentforge.core.errors import AgentForgeError
from agentforge.core.schemas import Message, Usage
from agentforge.llm.base import LLMProvider, LLMRequest, LLMResponse, StopReason
from agentforge.tools.base import SAFE_PERMISSIONS, ToolContext, ToolPermission
from agentforge.tools.registry import ToolRegistry

DEFAULT_MAX_STEPS = 10


class ToolLoopLimitError(AgentForgeError):
    """The model still wanted tools after ``max_steps`` LLM calls."""


@dataclass
class LoopResult:
    """The final answer plus the full transcript, for debugging and evaluation."""

    final: LLMResponse
    messages: list[Message]
    steps: int
    tool_calls: int
    usage: Usage = field(default_factory=Usage)

    @property
    def text(self) -> str:
        return self.final.text

    @property
    def truncated(self) -> bool:
        """True if the final answer was cut off by ``max_tokens``."""
        return self.final.stop_reason is StopReason.MAX_TOKENS


async def run_tool_loop(
    llm: LLMProvider,
    messages: list[Message],
    ctx: RunContext,
    *,
    tools: ToolRegistry | None = None,
    allowed: frozenset[ToolPermission] = SAFE_PERMISSIONS,
    max_steps: int = DEFAULT_MAX_STEPS,
    max_tokens: int = 1024,
    model: str | None = None,
) -> LoopResult:
    """Run the conversation until the model answers without calling tools."""
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")
    registry = tools or ToolRegistry()
    transcript = list(messages)
    tool_ctx = ToolContext(
        run_id=ctx.run_id,
        correlation_id=ctx.correlation_id,
        allowed=allowed,
        log=ctx.log,
        metadata=ctx.metadata,
    )
    usage = Usage()
    tool_calls = 0

    for step in range(1, max_steps + 1):
        response = await llm.complete(
            LLMRequest(
                messages=transcript, tools=registry.specs(), max_tokens=max_tokens, model=model
            )
        )
        usage = usage + response.usage
        ctx.add_usage(response.usage)
        transcript.append(response.message)

        if not response.wants_tools:
            ctx.log.info(
                "loop.finished", steps=step, tool_calls=tool_calls, stop_reason=response.stop_reason
            )
            return LoopResult(
                final=response,
                messages=transcript,
                steps=step,
                tool_calls=tool_calls,
                usage=usage,
            )

        calls = response.message.tool_calls
        tool_calls += len(calls)
        ctx.log.info("loop.tools", step=step, tools=[c.name for c in calls])
        # Independent calls in one turn run concurrently; results keep the call order.
        results = await asyncio.gather(*(registry.dispatch(c, tool_ctx) for c in calls))
        transcript.extend(Message.tool(r) for r in results)

    ctx.log.error("loop.limit", max_steps=max_steps, tool_calls=tool_calls)
    raise ToolLoopLimitError(
        f"model still requested tools after {max_steps} steps ({tool_calls} tool calls)"
    )
