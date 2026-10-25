"""Web tools for the Research agent: ``web_search`` and ``fetch_page``.

``fetch_page`` is :class:`HttpFetchTool` (public hosts only, redirects checked, size cap) that
also **remembers what the model was shown**: the text of every page it returns is stored in
``ctx.metadata["sources"]`` under the page's final URL. The Research agent later checks each
citation's quote against that record, so a citation can only point at a page that was really
fetched, and only quote words the model really saw.

``web_search`` asks a :class:`SearchProvider` (Brave Search ships here; the protocol is one
method, so another engine is a few lines) and returns titles, URLs and snippets. A snippet is
a lead, not evidence: it is not recorded, so it cannot be cited until the page is fetched.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

import httpx2
from pydantic import BaseModel, Field

from agentforge.tools.base import BaseTool, ToolContext, ToolError, ToolPermission
from agentforge.tools.builtin.http import USER_AGENT, HttpFetchArgs, HttpFetchTool

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
SOURCES_KEY = "sources"
MAX_PAGES_KEY = "max_pages"
FETCHED_KEY = "pages_fetched"
MAX_SEARCH_RESULTS = 20


def normalise_url(url: str) -> str:
    """A URL as used for matching: no fragment, no trailing slash, lower-case scheme and host."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


class SearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""


class SearchProvider(Protocol):
    async def search(self, query: str, count: int) -> list[SearchResult]: ...


class BraveSearch:
    """Brave Search API (https://api.search.brave.com). Needs a subscription token."""

    def __init__(self, api_key: str, *, client: httpx2.AsyncClient | None = None) -> None:
        self._key = api_key
        self._client = client

    async def search(self, query: str, count: int) -> list[SearchResult]:
        client = self._client or httpx2.AsyncClient(timeout=20.0)
        try:
            response = await client.get(
                BRAVE_URL,
                params={"q": query, "count": min(max(count, 1), MAX_SEARCH_RESULTS)},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._key,
                    "User-Agent": USER_AGENT,
                },
            )
        except httpx2.HTTPError as exc:
            raise ToolError(
                f"The search service could not be reached ({type(exc).__name__})."
            ) from exc
        finally:
            if self._client is None:
                await client.aclose()
        if response.status_code in (401, 403):
            raise ToolError("The search service rejected the API key.")
        if response.status_code == 429:
            raise ToolError("The search service is rate-limiting requests; try again later.")
        if response.status_code >= 400:
            raise ToolError(f"The search service answered HTTP {response.status_code}.")
        try:
            rows = response.json().get("web", {}).get("results", [])
        except ValueError as exc:
            raise ToolError("The search service returned something that is not JSON.") from exc
        found = []
        for row in rows:
            if isinstance(row, dict) and row.get("url") and row.get("title"):
                found.append(
                    SearchResult(
                        title=str(row["title"]),
                        url=str(row["url"]),
                        snippet=str(row.get("description") or ""),
                    )
                )
        return found[:count]


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=2, max_length=300, description="What to search for")


class WebSearchTool(BaseTool[WebSearchArgs]):
    name = "web_search"
    description = (
        "Search the web. Returns titles, URLs and short snippets. A snippet is only a lead: "
        "call fetch_page on a result to read it before citing anything."
    )
    args_model = WebSearchArgs
    permission = ToolPermission.NETWORK
    timeout_s = 30.0

    def __init__(self, provider: SearchProvider, *, results: int = 8) -> None:
        super().__init__()
        self._provider = provider
        self._results = results

    async def run(self, args: WebSearchArgs, ctx: ToolContext) -> str:
        found = await self._provider.search(args.query, self._results)
        if not found:
            return "No results. Try different words."
        lines = []
        for n, r in enumerate(found, start=1):
            snippet = f"\n   {r.snippet.strip()[:300]}" if r.snippet.strip() else ""
            lines.append(f"{n}. {r.title.strip()}\n   {r.url}{snippet}")
        return "\n".join(lines)


class FetchPageTool(HttpFetchTool):
    """``http_fetch`` under the name ``fetch_page`` that records the text it returns."""

    name = "fetch_page"
    description = (
        "Fetch a public web page and return its text (HTML is converted to plain text). "
        "Only text returned by this tool can be quoted as evidence."
    )

    async def run(self, args: HttpFetchArgs, ctx: ToolContext) -> str:
        limit = ctx.metadata.get(MAX_PAGES_KEY)
        count = ctx.metadata.get(FETCHED_KEY, 0)
        if limit is not None and count >= limit:
            raise ToolError("Page limit reached. Answer from what you have read.")
        ctx.metadata[FETCHED_KEY] = count + 1
        text = await super().run(args, ctx)
        record_source(ctx.metadata, text, requested=args.url, limit=self.max_output_chars)
        return text


def record_source(metadata: dict[str, Any], rendered: str, *, requested: str, limit: int) -> None:
    """Store a fetched page (``URL: <final url>\\n\\n<text>``) as the model saw it."""
    final = requested
    body = rendered
    if rendered.startswith("URL: "):
        head, _, body = rendered.partition("\n\n")
        final = head.removeprefix("URL: ").strip() or requested
    sources: dict[str, str] = metadata.setdefault(SOURCES_KEY, {})
    visible = body[:limit]
    for key in {normalise_url(final), normalise_url(requested)}:
        sources[key] = visible


def web_tools(
    *,
    provider: SearchProvider | None,
    results: int = 8,
    allowed_hosts: Iterable[str] | None = None,
    max_bytes: int = 300_000,
) -> list[BaseTool[Any]]:
    """The tools a researching agent gets: fetch_page, plus web_search if there is a provider."""
    tools: list[BaseTool[Any]] = [FetchPageTool(allowed_hosts=allowed_hosts, max_bytes=max_bytes)]
    if provider is not None:
        tools.append(WebSearchTool(provider, results=results))
    return tools
