"""Input and output contracts for the Research agent."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from agentforge.core.schemas import AgentInput, AgentOutput, Contract

type Confidence = Literal["none", "low", "medium", "high"]


class ResearchInput(AgentInput):
    """A question, and where the answer may come from."""

    question: str = Field(min_length=5, description="What to find out")
    sources: dict[str, str] = Field(
        default_factory=dict,
        description="Documents to read: name -> text. Cited by name",
    )
    urls: list[str] = Field(
        default_factory=list, description="Web pages to fetch and read first (public hosts only)"
    )
    search: bool = Field(
        default=False, description="Let the agent search the web and fetch pages (needs a key)"
    )
    max_sources: int = Field(default=6, ge=1, le=15, description="Most pages the agent may fetch")
    instructions: str | None = Field(
        default=None, description="Style or scope, e.g. 'two paragraphs, for an engineer'"
    )

    @model_validator(mode="after")
    def _has_somewhere_to_look(self) -> Self:
        if not (self.sources or self.urls or self.search):
            raise ValueError("provide 'sources', 'urls', or set 'search' to true")
        if len(self.urls) > self.max_sources:
            raise ValueError(f"{len(self.urls)} urls is more than max_sources ({self.max_sources})")
        return self


class Citation(Contract):
    """One piece of evidence: a short quote from one source."""

    n: int = Field(ge=1, description="The number used as [n] in the answer")
    source: str = Field(min_length=1, description="The document name or page URL it comes from")
    quote: str = Field(
        min_length=20,
        max_length=400,
        description="Words copied exactly from that source that support the claim",
    )


class SourceUsed(Contract):
    n: int
    source: str
    kind: Literal["document", "web"]


class ResearchOutput(AgentOutput):
    answer: str = Field(
        min_length=1,
        description="The answer. Each claim ends with the citation number(s), like [1] or [1][3]",
    )
    confidence: Confidence = Field(
        description="none = the sources do not answer the question; low, medium, high otherwise"
    )
    citations: list[Citation] = Field(default_factory=list)
    gaps: list[str] = Field(
        default_factory=list, description="What the sources do not say or disagree about"
    )
    # Filled in by AgentForge from the checked citations.
    sources: list[SourceUsed] = Field(default_factory=list, description="Cited sources, in order")
    fetched: list[str] = Field(default_factory=list, description="Every page that was fetched")
