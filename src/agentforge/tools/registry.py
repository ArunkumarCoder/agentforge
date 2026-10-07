"""ToolRegistry: the set of tools an agent may offer to the model."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from agentforge.core.errors import AgentForgeError
from agentforge.core.schemas import ToolCall, ToolResult
from agentforge.llm.base import ToolSpec
from agentforge.tools.base import BaseTool, ToolContext, ToolPermission

AnyTool = BaseTool[Any]


class DuplicateToolError(AgentForgeError):
    """Two tools with the same name were registered."""


class ToolRegistry:
    """Holds tools by name, exposes their specs, and dispatches calls."""

    def __init__(self, tools: Iterable[AnyTool] = ()) -> None:
        self._tools: dict[str, AnyTool] = {}
        for t in tools:
            self.register(t)

    def register(self, tool: AnyTool) -> None:
        if tool.name in self._tools:
            raise DuplicateToolError(f"a tool named '{tool.name}' is already registered")
        self._tools[tool.name] = tool

    def get(self, name: str) -> AnyTool | None:
        return self._tools.get(name)

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[ToolSpec]:
        """Tool specs for an ``LLMRequest``, in a stable (sorted) order."""
        return [self._tools[name].spec() for name in self.names]

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        """A registry with only the named tools (unknown names raise ``KeyError``)."""
        return ToolRegistry(self._tools[name] for name in names)

    def with_permissions(self, allowed: Iterable[ToolPermission]) -> ToolRegistry:
        """A registry with only the tools whose permission is in ``allowed``."""
        allowed_set = set(allowed)
        return ToolRegistry(t for t in self._tools.values() if t.permission in allowed_set)

    async def dispatch(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Run the tool named in ``call``. Unknown tools become an error result."""
        tool = self._tools.get(call.name)
        if tool is None:
            ctx.log.warning("tool.unknown", tool=call.name, tool_call_id=call.id)
            return ToolResult(
                tool_call_id=call.id,
                name=call.name,
                content=f"Unknown tool '{call.name}'. Available tools: {', '.join(self.names)}.",
                is_error=True,
            )
        return await tool.invoke(call, ctx)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self) -> Iterator[AnyTool]:
        return iter(self._tools.values())
