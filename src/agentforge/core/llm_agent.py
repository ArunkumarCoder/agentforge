"""LLMAgent: the base class for every LLM-backed agent.

An agent package looks like this::

    agents/code_review/
    ├── __init__.py
    ├── agent.py      # class CodeReviewAgent(LLMAgent[ReviewInput, ReviewOutput])
    ├── schema.py     # Pydantic input/output models
    ├── prompt.md     # the system prompt (versioned with the code)
    └── evals/        # golden cases: *.json

``LLMAgent`` does the rest:

1. Builds the system prompt from ``prompt.md``. ``{{output_schema}}`` is
   replaced with the output model's JSON Schema, and other ``{{placeholders}}``
   with values from ``prompt_vars``.
2. Builds the user message (``build_user_message``) and the tools for this
   input (``build_tools``).
3. Runs the tool-calling loop.
4. Parses the final answer into the output model. If it is invalid, the
   errors are sent back **once** so the model can fix them; a second failure
   becomes an ``output_validation`` error on the run.
5. Lets the subclass clean up the result (``postprocess``).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import ClassVar

from agentforge.config import get_settings
from agentforge.core.agent import BaseAgent, RunContext
from agentforge.core.errors import AgentDefinitionError, AgentOutputError
from agentforge.core.loop import run_tool_loop
from agentforge.core.schemas import AgentInput, AgentOutput, Message
from agentforge.core.structured import OutputParseError, parse_model_output, schema_for_prompt
from agentforge.llm.base import LLMProvider, LLMRequest
from agentforge.llm.factory import create_provider
from agentforge.tools.base import SAFE_PERMISSIONS, ToolPermission
from agentforge.tools.registry import ToolRegistry

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")

REPAIR_MESSAGE = (
    "Your previous reply could not be used: {error}\n\n"
    "Reply again with ONLY the corrected JSON object that matches the schema. "
    "No explanation, no markdown fence."
)


class LLMAgent[InT: AgentInput, OutT: AgentOutput](BaseAgent[InT, OutT]):
    """Base for agents that think with an LLM. See the module docstring."""

    prompt_file: ClassVar[str] = "prompt.md"
    max_steps: ClassVar[int] = 8
    max_tokens: ClassVar[int] = 4096
    allowed_permissions: ClassVar[frozenset[ToolPermission]] = SAFE_PERMISSIONS
    model: ClassVar[str | None] = None  # override the provider's default model

    def __init__(self, llm: LLMProvider | None = None) -> None:
        super().__init__()
        self.llm = llm or create_provider(get_settings())
        self._prompt_template = self._load_prompt()

    # ------------------------------------------------------------------ hooks

    def build_user_message(self, data: InT, ctx: RunContext) -> str:
        """The task as the model sees it. Default: the input as JSON."""
        return data.model_dump_json(indent=2, exclude_none=True)

    def build_tools(self, data: InT, ctx: RunContext) -> ToolRegistry | None:
        """Tools for this run. Default: none."""
        return None

    def prompt_vars(self, data: InT) -> dict[str, str]:
        """Extra values for ``{{placeholders}}`` in prompt.md."""
        return {}

    def postprocess(self, output: OutT, data: InT, ctx: RunContext) -> OutT:
        """Adjust or check the parsed output (e.g. sort, drop invalid items)."""
        return output

    # ------------------------------------------------------------------ run

    def system_prompt(self, data: InT) -> str:
        values = {"output_schema": schema_for_prompt(self.output_model), **self.prompt_vars(data)}

        def fill(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in values:
                raise AgentDefinitionError(f"{self.name}: prompt placeholder '{key}' has no value")
            return values[key]

        return _PLACEHOLDER.sub(fill, self._prompt_template)

    async def execute(self, data: InT, ctx: RunContext) -> OutT:
        messages = [
            Message.system(self.system_prompt(data)),
            Message.user(self.build_user_message(data, ctx)),
        ]
        result = await run_tool_loop(
            self.llm,
            messages,
            ctx,
            tools=self.build_tools(data, ctx),
            allowed=self.allowed_permissions,
            max_steps=self.max_steps,
            max_tokens=self.max_tokens,
            model=self.model,
        )
        ctx.metadata.update(steps=result.steps, tool_calls=result.tool_calls)
        try:
            output = parse_model_output(result.text, self.output_model)
        except OutputParseError as first:
            ctx.log.warning("agent.output.repair", error_message=str(first))
            output = await self._repair(result.messages, first, ctx)
        return self.postprocess(output, data, ctx)

    async def _repair(
        self, transcript: list[Message], error: OutputParseError, ctx: RunContext
    ) -> OutT:
        messages = [*transcript, Message.user(REPAIR_MESSAGE.format(error=error))]
        response = await self.llm.complete(
            LLMRequest(messages=messages, max_tokens=self.max_tokens, model=self.model)
        )
        ctx.add_usage(response.usage)
        try:
            return parse_model_output(response.text, self.output_model)
        except OutputParseError as second:
            raise AgentOutputError(f"invalid output after one repair attempt: {second}") from second

    # ------------------------------------------------------------------ internals

    def _load_prompt(self) -> str:
        path = Path(inspect.getfile(type(self))).with_name(self.prompt_file)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgentDefinitionError(f"{self.name}: cannot read prompt file {path}") from exc
        if not text.strip():
            raise AgentDefinitionError(f"{self.name}: prompt file {path} is empty")
        return text
