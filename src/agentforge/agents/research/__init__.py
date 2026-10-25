"""Agent 11 — Research."""

from agentforge.agents.research.agent import ResearchAgent, ResearchError, citation_problems
from agentforge.agents.research.schema import (
    Citation,
    Confidence,
    ResearchInput,
    ResearchOutput,
    SourceUsed,
)

__all__ = [
    "Citation",
    "Confidence",
    "ResearchAgent",
    "ResearchError",
    "ResearchInput",
    "ResearchOutput",
    "SourceUsed",
    "citation_problems",
]
