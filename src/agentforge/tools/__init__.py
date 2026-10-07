"""Tools the LLM can call: BaseTool, the @tool decorator and ToolRegistry."""

from agentforge.tools.base import (
    SAFE_PERMISSIONS,
    BaseTool,
    ToolContext,
    ToolDefinitionError,
    ToolError,
    ToolPermission,
)
from agentforge.tools.decorator import FunctionTool, tool
from agentforge.tools.registry import DuplicateToolError, ToolRegistry

__all__ = [
    "SAFE_PERMISSIONS",
    "BaseTool",
    "DuplicateToolError",
    "FunctionTool",
    "ToolContext",
    "ToolDefinitionError",
    "ToolError",
    "ToolPermission",
    "ToolRegistry",
    "tool",
]
