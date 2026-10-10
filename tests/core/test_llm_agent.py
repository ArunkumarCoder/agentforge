"""Tests for LLMAgent: prompt rendering, tool loop, output parsing and the one repair attempt."""

import pytest

from agentforge.core import (
    AgentDefinitionError,
    AgentInput,
    AgentOutput,
    ErrorKind,
    RunContext,
    ToolCall,
)
from agentforge.core.llm_agent import LLMAgent
from agentforge.core.structured import OutputCheckError
from agentforge.llm import FakeProvider, LLMRequest
from agentforge.tools import ToolRegistry, tool


class Topic(AgentInput):
    topic: str


class Haiku(AgentOutput):
    lines: list[str]


@tool
def syllables(word: str) -> int:
    """Count syllables (roughly)."""
    return max(1, sum(ch in "aeiou" for ch in word.lower()))


class HaikuAgent(LLMAgent[Topic, Haiku]):
    name = "haiku"
    description = "Writes a haiku about a topic."
    input_model = Topic
    output_model = Haiku
    prompt_file = "haiku_prompt.md"

    def prompt_vars(self, data: Topic) -> dict[str, str]:
        return {"style": "calm"}

    def build_tools(self, data: Topic, ctx: RunContext) -> ToolRegistry:
        return ToolRegistry([syllables])

    def postprocess(self, output: Haiku, data: Topic, ctx: RunContext) -> Haiku:
        return Haiku(lines=[line.strip() for line in output.lines])


VALID = '{"lines": [" old pond ", "a frog jumps in", "water sound"]}'


async def test_prompt_rendering_and_success() -> None:
    llm = FakeProvider(script=[VALID])
    result = await HaikuAgent(llm).arun({"topic": "frogs"})
    assert result.ok
    assert result.output == Haiku(lines=["old pond", "a frog jumps in", "water sound"])
    request: LLMRequest = llm.requests[0]
    system = request.messages[0].content
    assert system.startswith("Write a calm haiku.")
    assert '"lines"' in system  # output schema was inserted
    assert '"topic": "frogs"' in request.messages[1].content  # default user message = input JSON
    assert [t.name for t in request.tools] == ["syllables"]


async def test_tools_are_used_before_answering() -> None:
    llm = FakeProvider(
        script=[ToolCall(id="c1", name="syllables", arguments={"word": "frog"}), VALID]
    )
    result = await HaikuAgent(llm).arun({"topic": "frogs"})
    assert result.ok
    assert llm.requests[1].messages[-1].content == "1"


async def test_invalid_output_is_repaired_once(logs: object) -> None:
    llm = FakeProvider(script=['{"lines": "not a list"}', VALID])
    result = await HaikuAgent(llm).arun({"topic": "frogs"})
    assert result.ok
    repair = llm.requests[1].messages[-1].content
    assert "could not be used" in repair
    assert "lines: Input should be a valid list" in repair
    assert llm.requests[1].tools == []  # no tools during the repair
    assert result.usage.total_tokens > 0


async def test_second_failure_is_an_output_validation_error() -> None:
    llm = FakeProvider(script=["sorry, no haiku today", "still no"])
    result = await HaikuAgent(llm).arun({"topic": "frogs"})
    assert not result.ok
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "after one repair attempt" in result.error.message


class ThreeLineHaikuAgent(HaikuAgent):
    async def check(self, output: Haiku, data: Topic, ctx: RunContext) -> None:
        if len(output.lines) != 3:
            raise OutputCheckError(f"A haiku has 3 lines, not {len(output.lines)}.")


async def test_check_failure_triggers_the_repair() -> None:
    llm = FakeProvider(script=['{"lines": ["just one"]}', VALID])
    result = await ThreeLineHaikuAgent(llm).arun({"topic": "frogs"})
    assert result.ok
    assert "A haiku has 3 lines, not 1." in llm.requests[1].messages[-1].content


async def test_check_failure_twice_is_an_output_validation_error() -> None:
    llm = FakeProvider(script=['{"lines": ["one"]}', '{"lines": ["one", "two"]}'])
    result = await ThreeLineHaikuAgent(llm).arun({"topic": "frogs"})
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert "not 2" in result.error.message


async def test_repair_budget_allows_several_corrections() -> None:
    class Patient(ThreeLineHaikuAgent):
        max_repairs = 2

    llm = FakeProvider(script=['{"lines": ["one"]}', '{"lines": ["one", "two"]}', VALID])
    result = await Patient(llm).arun({"topic": "frogs"})
    assert result.ok
    last = llm.requests[2].messages
    assert [m.role.value for m in last[-4:]] == ["assistant", "user", "assistant", "user"]
    assert "not 2" in last[-1].content


async def test_unknown_placeholder_is_a_definition_error() -> None:
    class BadPrompt(HaikuAgent):
        prompt_file = "haiku_bad_prompt.md"

    result = await BadPrompt(FakeProvider(script=[VALID])).arun({"topic": "x"})
    assert result.error is not None
    assert "placeholder 'mood' has no value" in result.error.message


def test_missing_or_empty_prompt_file() -> None:
    class NoPrompt(HaikuAgent):
        prompt_file = "does_not_exist.md"

    with pytest.raises(AgentDefinitionError, match="cannot read prompt file"):
        NoPrompt(FakeProvider())

    class EmptyPrompt(HaikuAgent):
        prompt_file = "haiku_empty_prompt.md"

    with pytest.raises(AgentDefinitionError, match="is empty"):
        EmptyPrompt(FakeProvider())


def test_default_provider_comes_from_settings() -> None:
    assert isinstance(HaikuAgent().llm, FakeProvider)
