"""Exception hierarchy for AgentForge."""


class AgentForgeError(Exception):
    """Base class for all AgentForge errors."""


class AgentDefinitionError(AgentForgeError):
    """An agent class is declared incorrectly (bad name, missing models, ...)."""


class AgentOutputError(AgentForgeError):
    """An agent produced output that does not match its output model."""
