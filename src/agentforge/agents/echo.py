"""Echo: the smallest possible agent, used to check that discovery, the CLI and the API work."""

from __future__ import annotations

from pydantic import Field

from agentforge.core.agent import BaseAgent, RunContext
from agentforge.core.registry import register_agent
from agentforge.core.schemas import AgentInput, AgentOutput


class EchoInput(AgentInput):
    text: str = Field(min_length=1, description="Text to echo back")
    upper: bool = Field(default=False, description="Return the text in upper case")


class EchoOutput(AgentOutput):
    text: str
    length: int


@register_agent
class EchoAgent(BaseAgent[EchoInput, EchoOutput]):
    name = "echo"
    description = "Returns the text it is given. Useful for smoke tests; no LLM involved."
    tags = ("demo", "no-llm")
    input_model = EchoInput
    output_model = EchoOutput

    async def execute(self, data: EchoInput, ctx: RunContext) -> EchoOutput:
        text = data.text.upper() if data.upper else data.text
        return EchoOutput(text=text, length=len(text))
