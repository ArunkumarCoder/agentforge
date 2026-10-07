"""``@tool``: turn a typed function into a ``BaseTool``.

The argument model is built from the function signature; parameter
descriptions can be given with ``Annotated[int, Field(description=...)]``.
The first paragraph of the docstring becomes the tool description.
Sync and async functions both work. Add a parameter named ``ctx`` to receive
the ``ToolContext``; it is not shown to the model.

Example::

    @tool(permission=ToolPermission.READ)
    async def word_count(text: Annotated[str, Field(description="Text to count")]) -> int:
        \"\"\"Count the words in a text.\"\"\"
        return len(text.split())
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

from agentforge.tools.base import BaseTool, ToolContext, ToolDefinitionError, ToolPermission

CONTEXT_PARAM = "ctx"


class FunctionTool(BaseTool[BaseModel]):
    """A tool backed by a plain function (created by ``@tool``)."""

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        name: str,
        description: str,
        args_model: type[BaseModel],
        permission: ToolPermission,
        timeout_s: float,
        wants_context: bool,
    ) -> None:
        self.func = func
        self.name = name
        self.description = description
        self.args_model = args_model
        self.permission = permission
        self.timeout_s = timeout_s
        self._wants_context = wants_context
        super().__init__()

    async def run(self, args: BaseModel, ctx: ToolContext) -> Any:
        kwargs = {key: getattr(args, key) for key in type(args).model_fields}
        if self._wants_context:
            kwargs[CONTEXT_PARAM] = ctx
        if inspect.iscoroutinefunction(self.func):
            return await self.func(**kwargs)
        # Run sync functions in a thread so a slow one can't block the event loop.
        return await asyncio.to_thread(self.func, **kwargs)


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    permission: ToolPermission = ToolPermission.READ,
    timeout_s: float = 30.0,
) -> Any:
    """Decorator: ``@tool`` or ``@tool(permission=..., name=...)``."""

    def wrap(fn: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            fn,
            name=name or fn.__name__,
            description=description or _first_paragraph(fn),
            args_model=_args_model(fn),
            permission=permission,
            timeout_s=timeout_s,
            wants_context=CONTEXT_PARAM in inspect.signature(fn).parameters,
        )

    return wrap(func) if func is not None else wrap


def _args_model(fn: Callable[..., Any]) -> type[BaseModel]:
    hints = get_type_hints(fn, include_extras=True)
    fields: dict[str, Any] = {}
    for param in inspect.signature(fn).parameters.values():
        if param.name == CONTEXT_PARAM:
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            raise ToolDefinitionError(f"{fn.__name__}: *args/**kwargs are not supported in tools")
        if param.name not in hints:
            raise ToolDefinitionError(f"{fn.__name__}: parameter '{param.name}' needs a type hint")
        default = ... if param.default is param.empty else param.default
        fields[param.name] = (hints[param.name], default)
    model_name = "".join(part.title() for part in fn.__name__.split("_")) + "Args"
    model: type[BaseModel] = create_model(
        model_name, __config__=ConfigDict(extra="forbid"), **fields
    )
    return model


def _first_paragraph(fn: Callable[..., Any]) -> str:
    doc = inspect.getdoc(fn) or ""
    return doc.split("\n\n")[0].replace("\n", " ").strip()
