"""AgentRegistry: find agents by name, list their cards, and discover them.

Agents register themselves with a decorator::

    from agentforge.core.registry import register_agent

    @register_agent
    class CodeReviewAgent(BaseAgent[ReviewInput, ReviewOutput]):
        name = "code-review"
        ...

``discover()`` imports every module under ``agentforge.agents`` so that all
decorated agents are registered; the CLI, API and orchestrator then look agents
up by name instead of importing them one by one.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterator
from typing import Any

from agentforge.core.agent import BaseAgent
from agentforge.core.errors import AgentDefinitionError, AgentForgeError
from agentforge.core.schemas import AgentCard

AgentClass = type[BaseAgent[Any, Any]]
DEFAULT_PACKAGE = "agentforge.agents"


class UnknownAgentError(AgentForgeError):
    """No agent with the requested name is registered."""


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentClass] = {}

    def register(self, agent_cls: AgentClass) -> AgentClass:
        """Add an agent class. Usable as a decorator. The class definition is checked first."""
        agent_cls._check_definition()
        name: str = agent_cls.name
        existing = self._agents.get(name)
        if existing is not None and existing is not agent_cls:
            raise AgentDefinitionError(
                f"agent name '{name}' is used by both {existing.__qualname__} "
                f"and {agent_cls.__qualname__}"
            )
        self._agents[name] = agent_cls
        return agent_cls

    def get(self, name: str) -> AgentClass:
        try:
            return self._agents[name]
        except KeyError:
            available = ", ".join(self.names) or "none"
            raise UnknownAgentError(f"no agent named '{name}' (available: {available})") from None

    def create(self, name: str, **kwargs: Any) -> BaseAgent[Any, Any]:
        """Instantiate the named agent, passing ``kwargs`` to its constructor."""
        return self.get(name)(**kwargs)

    @property
    def names(self) -> list[str]:
        return sorted(self._agents)

    def cards(self) -> list[AgentCard]:
        """Agent cards in name order, for the CLI, the API and the orchestrator's router."""
        return [self._agents[name].card() for name in self.names]

    def discover(self, package: str = DEFAULT_PACKAGE) -> list[str]:
        """Import every module in ``package`` (recursively); returns the modules imported."""
        root = importlib.import_module(package)
        imported = [package]
        for info in pkgutil.walk_packages(root.__path__, prefix=f"{package}."):
            importlib.import_module(info.name)
            imported.append(info.name)
        return imported

    def __contains__(self, name: object) -> bool:
        return name in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self) -> Iterator[AgentClass]:
        return iter(self._agents[name] for name in self.names)


registry = AgentRegistry()
"""The process-wide registry used by ``@register_agent``, the CLI and the API."""


def register_agent(agent_cls: AgentClass) -> AgentClass:
    """Class decorator: add the agent to the default registry."""
    return registry.register(agent_cls)
