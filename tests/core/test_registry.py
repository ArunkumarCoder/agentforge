"""Tests for AgentRegistry and discovery of the agents package."""

import pytest

from agentforge.agents.echo import EchoAgent, EchoInput, EchoOutput
from agentforge.core import AgentDefinitionError, BaseAgent, RunContext
from agentforge.core.registry import AgentRegistry, UnknownAgentError, registry


class OtherEcho(BaseAgent[EchoInput, EchoOutput]):
    name = "echo"
    description = "Clashes with the real echo agent."
    input_model = EchoInput
    output_model = EchoOutput

    async def execute(self, data: EchoInput, ctx: RunContext) -> EchoOutput:
        return EchoOutput(text="", length=0)


class Shout(OtherEcho):
    name = "shout"
    description = "Another test agent."


def test_register_get_and_create() -> None:
    reg = AgentRegistry()
    assert reg.register(EchoAgent) is EchoAgent  # works as a decorator
    reg.register(Shout)
    assert reg.names == ["echo", "shout"]
    assert reg.get("echo") is EchoAgent
    assert isinstance(reg.create("echo"), EchoAgent)
    assert "echo" in reg
    assert len(reg) == 2
    assert list(reg) == [EchoAgent, Shout]


def test_registering_the_same_class_twice_is_harmless() -> None:
    reg = AgentRegistry()
    reg.register(EchoAgent)
    reg.register(EchoAgent)
    assert len(reg) == 1


def test_name_clash_is_rejected() -> None:
    reg = AgentRegistry()
    reg.register(EchoAgent)
    with pytest.raises(AgentDefinitionError, match="used by both EchoAgent and OtherEcho"):
        reg.register(OtherEcho)


def test_invalid_agent_is_rejected_at_registration() -> None:
    class BadName(Shout):
        name = "Bad Name"

    with pytest.raises(AgentDefinitionError, match="kebab-case"):
        AgentRegistry().register(BadName)


def test_unknown_agent_lists_available() -> None:
    reg = AgentRegistry()
    reg.register(EchoAgent)
    with pytest.raises(UnknownAgentError, match=r"no agent named 'nope' \(available: echo\)"):
        reg.get("nope")
    with pytest.raises(UnknownAgentError, match="available: none"):
        AgentRegistry().get("x")


def test_cards_are_sorted_and_complete() -> None:
    reg = AgentRegistry()
    reg.register(Shout)
    reg.register(EchoAgent)
    cards = reg.cards()
    assert [c.name for c in cards] == ["echo", "shout"]
    assert cards[0].tags == ["demo", "no-llm"]
    assert cards[0].input_schema["properties"]["text"]["description"] == "Text to echo back"


def test_discovery_finds_the_echo_agent() -> None:
    imported = registry.discover()
    assert "agentforge.agents.echo" in imported
    assert "echo" in registry
    assert registry.get("echo") is EchoAgent


async def test_echo_agent_runs() -> None:
    result = await EchoAgent().arun({"text": "hi there", "upper": True})
    assert result.ok
    assert result.output == EchoOutput(text="HI THERE", length=8)
