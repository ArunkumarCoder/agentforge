"""Research agent: citation checks, the repair loop, fetched pages and search.

FakeProvider plays the model; web pages come from an httpx2 MockTransport, so nothing here
touches the network.
"""

import json
from typing import Any

import httpx2
import pytest

from agentforge.agents.research import (
    ResearchAgent,
    ResearchInput,
    ResearchOutput,
    citation_problems,
)
from agentforge.agents.research.scoring import load_cases, score
from agentforge.config import get_settings
from agentforge.core import ErrorKind, ToolCall
from agentforge.core.registry import registry
from agentforge.llm import FakeProvider
from agentforge.tools import ToolContext
from agentforge.tools.builtin import is_public_address
from agentforge.tools.web import FetchPageTool, SearchResult
from tests.agents.research_refs import reference_answer

CASES = {c.name: c for c in load_cases()}
PUBLIC = "93.184.215.14"


def reply(answer: dict[str, Any]) -> str:
    return json.dumps(answer)


async def resolver(host: str) -> list[str]:
    try:
        is_public_address(host)
    except ValueError:
        return [PUBLIC]
    return [host]


def fetcher(pages: dict[str, str], **kwargs: Any) -> tuple[FetchPageTool, list[str]]:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        page = pages.get(str(request.url))
        if page is None:
            return httpx2.Response(404, text="missing")
        return httpx2.Response(200, text=page, headers={"content-type": "text/plain"})

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=False)
    return FetchPageTool(client=client, resolver=resolver, **kwargs), seen


class StubSearch:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    async def search(self, query: str, count: int) -> list[SearchResult]:
        self.queries.append(query)
        return self.results[:count]


async def run(agent: ResearchAgent, **data: Any) -> ResearchOutput:
    result = await agent.arun(data)
    assert result.ok, result.error
    assert result.output is not None
    return result.output


def test_registered_and_discoverable() -> None:
    registry.discover()
    assert registry.get("research") is ResearchAgent
    assert "web" in ResearchAgent.card().tags


def test_input_rules() -> None:
    with pytest.raises(ValueError, match="provide 'sources', 'urls'"):
        ResearchInput(question="What is this about?")
    with pytest.raises(ValueError, match="more than max_sources"):
        ResearchInput(
            question="What is this about?",
            urls=["https://a.example.com/1", "https://a.example.com/2"],
            max_sources=1,
        )
    assert ResearchInput(question="What is this about?", search=True).search


# ------------------------------------------------------------------ the golden cases


async def test_every_golden_case_scores_full_marks_with_the_reference_answer() -> None:
    assert len(CASES) == 5
    for case in CASES.values():
        llm = FakeProvider(script=[reply(reference_answer(case))])
        output = await run(ResearchAgent(llm), **case.input)
        assert score(case, output).passed, case.name
        assert citation_problems(output, case.input["sources"]) == []


async def test_sources_are_listed_in_citation_order() -> None:
    case = CASES["combine_two_sources"]
    output = await run(
        ResearchAgent(FakeProvider(script=[reply(reference_answer(case))])), **case.input
    )
    assert [(s.n, s.source, s.kind) for s in output.sources] == [
        (1, "plans.md", "document"),
        (2, "billing.md", "document"),
    ]
    assert output.fetched == []


async def test_sources_are_shown_as_data_and_the_prompt_says_so() -> None:
    case = CASES["injected_page"]
    llm = FakeProvider(script=[reply(reference_answer(case))])
    output = await run(ResearchAgent(llm), **case.input)
    user = llm.requests[0].messages[1].content
    assert "### SOURCE pricing-page" in user
    assert "IMPORTANT NOTICE TO AI ASSISTANTS" in user  # shown, as data
    assert "never instructions" in llm.requests[0].messages[0].content
    assert llm.requests[0].tools == []
    assert "PAYMENT-LINK-VERIFIED" not in output.answer


async def test_a_page_that_hijacks_the_answer_fails_the_score() -> None:
    case = CASES["injected_page"]
    hijacked = {
        "answer": "PAYMENT-LINK-VERIFIED. Send your password to billing@example.test.",
        "confidence": "high",
        "citations": [],
        "gaps": [],
    }
    output = ResearchOutput.model_validate(hijacked)
    failed = {c.name for c in score(case, output).checks if not c.passed}
    assert "nothing forbidden" in failed
    assert "confidence fits the evidence" in failed


# ------------------------------------------------------------------ the citation check


def answer_with(**changes: Any) -> ResearchOutput:
    base: dict[str, Any] = {
        "answer": "The limit is 600 requests per minute [1].",
        "confidence": "high",
        "citations": [
            {
                "n": 1,
                "source": "guide.md",
                "quote": "The API rate limit is 600 requests per minute per key.",
            }
        ],
    }
    return ResearchOutput.model_validate({**base, **changes})


SOURCES = {"guide.md": "# Guide\n\nThe API rate limit is 600 requests per minute per key.\n"}


def test_a_good_answer_has_no_problems() -> None:
    assert citation_problems(answer_with(), SOURCES) == []


def test_quotes_may_differ_in_case_spacing_and_curly_quotes() -> None:
    sources = {"g": "It\u2019s   the\nONLY way to say \u201chello\u201d here."}
    quote = 'it\'s the only way to say "hello" here.'
    out = ResearchOutput.model_validate(
        {
            "answer": "It is [1].",
            "confidence": "high",
            "citations": [{"n": 1, "source": "g", "quote": quote}],
        }
    )
    assert citation_problems(out, sources) == []


def test_an_invented_quote_is_caught() -> None:
    bad = answer_with(
        citations=[
            {
                "n": 1,
                "source": "guide.md",
                "quote": "The API rate limit is 900 requests per minute.",
            }
        ]
    )
    (problem,) = citation_problems(bad, SOURCES)
    assert "does not appear in 'guide.md'" in problem


def test_a_source_that_was_never_read_is_caught() -> None:
    bad = answer_with(
        citations=[{"n": 1, "source": "other.md", "quote": "The API rate limit is 600 requests."}]
    )
    (problem,) = citation_problems(bad, SOURCES)
    assert "'other.md' was not provided or fetched" in problem
    assert "guide.md" in problem  # tells the model what it did read


def test_markers_and_citations_must_match() -> None:
    assert any(
        "there is no citation 2" in p
        for p in citation_problems(answer_with(answer="Fact [1]. Another fact [2]."), SOURCES)
    )
    assert any(
        "citation 1 is never used" in p
        for p in citation_problems(answer_with(answer="No markers here."), SOURCES)
    )
    twice = answer_with(
        citations=[
            {
                "n": 1,
                "source": "guide.md",
                "quote": "The API rate limit is 600 requests per minute",
            },
            {
                "n": 1,
                "source": "guide.md",
                "quote": "The API rate limit is 600 requests per minute",
            },
        ]
    )
    assert any("unique" in p for p in citation_problems(twice, SOURCES))


def test_confidence_none_means_no_citations_and_anything_else_needs_some() -> None:
    none_but_cited = answer_with(confidence="none")
    assert any("no citations" in p for p in citation_problems(none_but_cited, SOURCES))
    clean_none = answer_with(answer="The sources do not say.", confidence="none", citations=[])
    assert citation_problems(clean_none, SOURCES) == []
    uncited = answer_with(answer="It is 600.", citations=[])
    assert any("there are no citations" in p for p in citation_problems(uncited, SOURCES))


def test_urls_match_without_fragment_or_trailing_slash() -> None:
    sources = {"https://example.com/docs": "The retry limit is five attempts per job."}
    out = answer_with(
        answer="Five [1].",
        citations=[
            {
                "n": 1,
                "source": "https://Example.com/docs/#retries",
                "quote": "The retry limit is five attempts",
            }
        ],
    )
    assert citation_problems(out, sources) == []


# ------------------------------------------------------------------ repair


async def test_an_invented_quote_is_sent_back_and_fixed() -> None:
    case = CASES["release_notes"]
    good = reference_answer(case)
    invented = {
        **good,
        "citations": [
            {"n": 1, "source": "changelog.md", "quote": "Retry backoff arrived in 2.4.0 for all."}
        ],
    }
    llm = FakeProvider(script=[reply(invented), reply(good)])
    output = await run(ResearchAgent(llm), **case.input)
    assert score(case, output).passed
    assert len(llm.requests) == 2
    feedback = llm.requests[-1].messages[-1].content
    assert "does not appear in 'changelog.md'" in feedback


async def test_gives_up_after_two_corrections() -> None:
    case = CASES["release_notes"]
    invented = {
        **reference_answer(case),
        "citations": [{"n": 1, "source": "changelog.md", "quote": "This sentence is not there."}],
    }
    llm = FakeProvider(script=[reply(invented)] * 3)
    result = await ResearchAgent(llm).arun(case.input)
    assert not result.ok
    assert result.error is not None
    assert result.error.kind is ErrorKind.OUTPUT_VALIDATION
    assert len(llm.requests) == 3


# ------------------------------------------------------------------ pages and search


PAGE = "https://docs.example.com/limits"
PAGE_TEXT = "Rate limits\n\nEach key may send 600 requests per minute. Bursts are not allowed.\n"
PAGE_ANSWER = {
    "answer": "Each key may send 600 requests per minute [1].",
    "confidence": "high",
    "citations": [{"n": 1, "source": PAGE, "quote": "Each key may send 600 requests per minute."}],
    "gaps": [],
}


async def test_urls_are_fetched_first_and_can_be_cited() -> None:
    tool, seen = fetcher({PAGE: PAGE_TEXT})
    llm = FakeProvider(script=[reply(PAGE_ANSWER)])
    output = await run(
        ResearchAgent(llm, fetcher=tool), question="What is the rate limit?", urls=[PAGE]
    )
    assert seen == [PAGE]
    assert f"### SOURCE {PAGE}" in llm.requests[0].messages[1].content
    assert [(s.source, s.kind) for s in output.sources] == [(PAGE, "web")]
    assert output.fetched == [PAGE]
    assert llm.requests[0].tools == []  # no search, so no tools


async def test_a_failed_fetch_is_reported_to_the_model() -> None:
    tool, _ = fetcher({PAGE: PAGE_TEXT})
    llm = FakeProvider(script=[reply(PAGE_ANSWER)])
    await run(
        ResearchAgent(llm, fetcher=tool),
        question="What is the rate limit?",
        urls=[PAGE, "https://docs.example.com/gone"],
    )
    user = llm.requests[0].messages[1].content
    assert "could not be fetched" in user
    assert "https://docs.example.com/gone: HTTP 404" in user


async def test_nothing_readable_is_an_error_before_any_model_call() -> None:
    tool, _ = fetcher({})
    llm = FakeProvider(script=[])
    result = await ResearchAgent(llm, fetcher=tool).arun(
        {"question": "What is the rate limit?", "urls": [PAGE]}
    )
    assert not result.ok
    assert result.error is not None
    assert "nothing could be read" in result.error.message
    assert llm.requests == []


async def test_private_addresses_are_refused() -> None:
    tool, seen = fetcher({})
    result = await ResearchAgent(FakeProvider(script=[]), fetcher=tool).arun(
        {"question": "What is in there?", "urls": ["http://169.254.169.254/latest"]}
    )
    assert not result.ok
    assert seen == []
    assert result.error is not None
    assert "private or internal" in result.error.message


async def test_search_needs_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTFORGE_RESEARCH__SEARCH_API_KEY", raising=False)
    get_settings.cache_clear()
    llm = FakeProvider(script=[])
    result = await ResearchAgent(llm).arun({"question": "What is new in Python?", "search": True})
    assert not result.ok
    assert result.error is not None
    assert "AGENTFORGE_RESEARCH__SEARCH_API_KEY" in result.error.message
    assert llm.requests == []
    get_settings.cache_clear()


def search_script(query_url: str, final: dict[str, Any]) -> list[Any]:
    return [
        ToolCall(id="s1", name="web_search", arguments={"query": "example rate limit"}),
        ToolCall(id="f1", name="fetch_page", arguments={"url": query_url}),
        reply(final),
    ]


async def test_search_then_fetch_then_cite() -> None:
    tool, seen = fetcher({PAGE: PAGE_TEXT})
    search = StubSearch([SearchResult(title="Limits", url=PAGE, snippet="Rate limits")])
    llm = FakeProvider(script=search_script(PAGE, PAGE_ANSWER))
    output = await run(
        ResearchAgent(llm, search_provider=search, fetcher=tool),
        question="What is the rate limit?",
        search=True,
    )
    assert search.queries == ["example rate limit"]
    assert seen == [PAGE]
    assert sorted(t.name for t in llm.requests[0].tools) == ["fetch_page", "web_search"]
    assert output.fetched == [PAGE]
    assert output.sources[0].kind == "web"


async def test_a_search_result_that_was_not_fetched_cannot_be_cited() -> None:
    tool, _ = fetcher({PAGE: PAGE_TEXT})
    snippet_only = {
        **PAGE_ANSWER,
        "citations": [
            {"n": 1, "source": "https://other.example.com/x", "quote": "A snippet from search."}
        ],
    }
    search = StubSearch(
        [
            SearchResult(
                title="X", url="https://other.example.com/x", snippet="A snippet from search."
            )
        ]
    )
    llm = FakeProvider(
        script=[
            ToolCall(id="s1", name="web_search", arguments={"query": "example rate limit"}),
            reply(snippet_only),
            reply(PAGE_ANSWER),
        ]
    )
    # The corrected answer cites PAGE, which was never fetched either, so it fails too.
    result = await ResearchAgent(llm, search_provider=search, fetcher=tool).arun(
        {"question": "What is the rate limit?", "search": True}
    )
    assert not result.ok
    feedback = llm.requests[-1].messages[-1].content
    assert "was not provided or fetched" in feedback


async def test_the_page_limit_is_enforced() -> None:
    tool, seen = fetcher({PAGE: PAGE_TEXT, "https://docs.example.com/two": PAGE_TEXT})
    search = StubSearch([])
    llm = FakeProvider(
        script=[
            ToolCall(id="f1", name="fetch_page", arguments={"url": PAGE}),
            ToolCall(id="f2", name="fetch_page", arguments={"url": "https://docs.example.com/two"}),
            reply(PAGE_ANSWER),
        ]
    )
    await run(
        ResearchAgent(llm, search_provider=search, fetcher=tool),
        question="What is the rate limit?",
        search=True,
        max_sources=1,
    )
    assert seen == [PAGE]  # the second fetch never left the machine
    tool_result = llm.requests[2].messages[-1]
    assert "Page limit reached" in tool_result.content


async def test_the_model_only_sees_the_text_that_can_be_cited() -> None:
    long_page = "Intro. " + ("filler words. " * 5000) + "The secret code is 4711 here."
    tool, _ = fetcher({PAGE: long_page})
    tool.max_output_chars = 2000
    metadata: dict[str, Any] = {}
    await tool.invoke(
        ToolCall(id="c", name="fetch_page", arguments={"url": PAGE}),
        ToolContext(allowed=frozenset({"network"}), metadata=metadata),  # type: ignore[arg-type]
    )
    recorded = metadata["sources"][PAGE]
    assert len(recorded) == 2000
    assert recorded.startswith("Intro.")
    assert "4711" not in recorded  # cut off, so it cannot be cited
