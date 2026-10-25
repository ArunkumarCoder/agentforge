"""web_search, the Brave client, and fetch_page's record of what it returned."""

from typing import Any

import httpx2
import pytest

from agentforge.core import ToolCall
from agentforge.tools import ToolContext
from agentforge.tools.base import ToolError
from agentforge.tools.web import (
    BraveSearch,
    FetchPageTool,
    SearchResult,
    WebSearchTool,
    normalise_url,
    record_source,
    web_tools,
)

BRAVE_BODY = {
    "web": {
        "results": [
            {"title": "One", "url": "https://one.example.com/a", "description": "First hit"},
            {"title": "Two", "url": "https://two.example.com/b"},
            {"title": "", "url": "https://skipped.example.com"},
            "junk",
        ]
    }
}


def brave(response: httpx2.Response | None = None) -> tuple[BraveSearch, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return response or httpx2.Response(200, json=BRAVE_BODY)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return BraveSearch("test-token", client=client), seen


async def test_brave_sends_the_token_and_parses_results() -> None:
    search, seen = brave()
    found = await search.search("loomstack retries", 5)
    assert [(r.title, r.url, r.snippet) for r in found] == [
        ("One", "https://one.example.com/a", "First hit"),
        ("Two", "https://two.example.com/b", ""),
    ]
    request = seen[0]
    assert request.headers["X-Subscription-Token"] == "test-token"
    assert request.url.params["q"] == "loomstack retries"
    assert request.url.params["count"] == "5"


async def test_brave_limits_the_count() -> None:
    search, seen = brave()
    await search.search("anything", 500)
    assert seen[0].url.params["count"] == "20"


@pytest.mark.parametrize(
    ("status", "text"),
    [(401, "rejected the API key"), (429, "rate-limiting"), (500, "HTTP 500")],
)
async def test_brave_errors_become_tool_errors(status: int, text: str) -> None:
    search, _ = brave(httpx2.Response(status))
    with pytest.raises(ToolError, match=text):
        await search.search("anything", 3)


async def test_brave_bad_json_and_network_errors() -> None:
    search, _ = brave(httpx2.Response(200, text="<html>"))
    with pytest.raises(ToolError, match="not JSON"):
        await search.search("anything", 3)

    def boom(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("down")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(boom))
    with pytest.raises(ToolError, match="could not be reached"):
        await BraveSearch("k", client=client).search("anything", 3)


class Stub:
    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results

    async def search(self, query: str, count: int) -> list[SearchResult]:
        return self.results[:count]


async def call(
    tool: Any, name: str, args: dict[str, Any], meta: dict[str, Any]
) -> tuple[str, bool]:
    result = await tool.invoke(
        ToolCall(id="c", name=name, arguments=args),
        ToolContext(allowed=frozenset({"network"}), metadata=meta),  # type: ignore[arg-type]
    )
    return result.content, result.is_error


async def test_web_search_tool_formats_results() -> None:
    tool = WebSearchTool(Stub([SearchResult(title="A", url="https://a.example.com", snippet="s")]))
    text, error = await call(tool, "web_search", {"query": "something"}, {})
    assert not error
    assert text == "1. A\n   https://a.example.com\n   s"
    empty, _ = await call(WebSearchTool(Stub([])), "web_search", {"query": "something"}, {})
    assert empty.startswith("No results")


async def test_web_search_does_not_record_sources() -> None:
    meta: dict[str, Any] = {}
    tool = WebSearchTool(Stub([SearchResult(title="A", url="https://a.example.com", snippet="s")]))
    await call(tool, "web_search", {"query": "something"}, meta)
    assert "sources" not in meta  # a snippet is a lead, not evidence


def test_normalise_url() -> None:
    assert normalise_url("HTTPS://Example.COM/Docs/#top") == "https://example.com/Docs"
    assert normalise_url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert normalise_url(" https://example.com/ ") == "https://example.com"


def test_record_source_keeps_both_spellings_of_a_redirected_url() -> None:
    meta: dict[str, Any] = {}
    record_source(
        meta,
        "URL: https://b.example.com/new\n\nHello",
        requested="https://a.example.com/old/",
        limit=100,
    )
    assert meta["sources"] == {
        "https://b.example.com/new": "Hello",
        "https://a.example.com/old": "Hello",
    }


def test_web_tools_includes_search_only_with_a_provider() -> None:
    assert [t.name for t in web_tools(provider=None)] == ["fetch_page"]
    both = web_tools(provider=Stub([]))
    assert sorted(t.name for t in both) == ["fetch_page", "web_search"]
    assert isinstance(both[0], FetchPageTool)
