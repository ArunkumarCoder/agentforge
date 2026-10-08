"""http_fetch: SSRF protection, redirects, limits and HTML-to-text."""

from typing import Any

import httpx2
import pytest

from agentforge.core import ToolCall
from agentforge.tools import ToolContext
from agentforge.tools.builtin import HttpFetchTool, html_to_text, is_public_address

PUBLIC = "93.184.215.14"
DNS = {
    "example.com": [PUBLIC],
    "docs.example.com": [PUBLIC],
    "internal.example.com": ["10.0.0.5"],
    "mixed.example.com": [PUBLIC, "127.0.0.1"],
    "metadata.example.com": ["169.254.169.254"],
}


async def fake_resolver(host: str) -> list[str]:
    if host in DNS:
        return DNS[host]
    try:
        is_public_address(host)  # literal IPs resolve to themselves
    except ValueError:
        raise OSError("no such host") from None
    return [host]


def make_tool(routes: dict[str, httpx2.Response], **kwargs: Any) -> tuple[HttpFetchTool, list[str]]:
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return routes.get(str(request.url), httpx2.Response(404, text="not found"))

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=False)
    return HttpFetchTool(client=client, resolver=fake_resolver, **kwargs), seen


async def fetch(tool: HttpFetchTool, url: str) -> tuple[str, bool]:
    result = await tool.invoke(
        ToolCall(id="c", name="http_fetch", arguments={"url": url}),
        ToolContext(allowed=frozenset({"network"})),
    )  # type: ignore[arg-type]
    return result.content, result.is_error


PAGE = """<!doctype html><html><head><title>t</title><style>p{color:red}</style></head>
<body><h1>Release notes</h1><script>steal()</script><p>Version <b>2.0</b> &amp; more.</p>
<ul><li>Faster</li><li>Safer</li></ul></body></html>"""


async def test_html_page_becomes_text() -> None:
    tool, _ = make_tool(
        {
            "https://example.com/notes": httpx2.Response(
                200, text=PAGE, headers={"content-type": "text/html"}
            )
        }
    )
    text, error = await fetch(tool, "https://example.com/notes")
    assert not error
    assert (
        text
        == "URL: https://example.com/notes\n\nRelease notes\nVersion 2.0 & more.\nFaster\nSafer"
    )


async def test_plain_text_and_json_pass_through() -> None:
    tool, _ = make_tool({"https://example.com/a.json": httpx2.Response(200, json={"ok": True})})
    text, _ = await fetch(tool, "https://example.com/a.json")
    assert text.endswith('{"ok":true}')


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://localhost/admin", "private or internal"),
        ("http://printer.local/", "private or internal"),
        ("http://127.0.0.1/", "private or internal"),
        ("http://10.1.2.3/", "private or internal"),
        ("http://[::1]/", "private or internal"),
        ("http://internal.example.com/", "private or internal"),
        ("http://mixed.example.com/", "private or internal"),
        ("http://metadata.example.com/latest/meta-data", "private or internal"),
        ("http://169.254.169.254/latest/meta-data", "private or internal"),
        ("file:///etc/passwd", "Only http and https"),
        ("ftp://example.com/x", "Only http and https"),
        ("https://example.com:8443/", "standard ports"),
        ("https:///nohost", "Invalid URL"),
        ("https://nonexistent.invalid/", "Could not resolve"),
    ],
)
async def test_blocked_urls(url: str, message: str) -> None:
    tool, seen = make_tool({})
    text, error = await fetch(tool, url)
    assert error
    assert message in text
    assert seen == []  # no request was ever sent


async def test_redirect_to_internal_address_is_blocked() -> None:
    tool, seen = make_tool(
        {
            "https://example.com/go": httpx2.Response(
                302, headers={"location": "http://169.254.169.254/"}
            )
        }
    )
    text, error = await fetch(tool, "https://example.com/go")
    assert error and "private or internal" in text
    assert seen == ["https://example.com/go"]


async def test_safe_redirect_is_followed() -> None:
    tool, seen = make_tool(
        {
            "https://example.com/old": httpx2.Response(301, headers={"location": "/new"}),
            "https://example.com/new": httpx2.Response(200, text="moved here"),
        }
    )
    text, error = await fetch(tool, "https://example.com/old")
    assert not error
    assert text == "URL: https://example.com/new\n\nmoved here"
    assert seen == ["https://example.com/old", "https://example.com/new"]


async def test_redirect_loop() -> None:
    tool, _ = make_tool(
        {"https://example.com/loop": httpx2.Response(302, headers={"location": "/loop"})}
    )
    text, error = await fetch(tool, "https://example.com/loop")
    assert error and "Too many redirects" in text


async def test_allow_list() -> None:
    routes = {"https://docs.example.com/": httpx2.Response(200, text="docs")}
    tool, _ = make_tool(routes, allowed_hosts=["docs.example.com"])
    assert (await fetch(tool, "https://docs.example.com/"))[0].endswith("docs")
    text, error = await fetch(tool, "https://example.com/")
    assert error and "not on the allow-list" in text


async def test_size_cap_and_http_errors() -> None:
    tool, _ = make_tool(
        {
            "https://example.com/big": httpx2.Response(200, text="y" * 5000),
            "https://example.com/missing": httpx2.Response(404, text="gone"),
        },
        max_bytes=1000,
    )
    text, _ = await fetch(tool, "https://example.com/big")
    body = text.split("\n\n", 1)[1].splitlines()[0]
    assert body == "y" * 1000
    assert text.endswith("[cut at 1000 bytes]")
    text, error = await fetch(tool, "https://example.com/missing")
    assert error and "HTTP 404" in text


async def test_transport_errors_are_reported() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    tool = HttpFetchTool(client=client, resolver=fake_resolver)
    text, error = await fetch(tool, "https://example.com/")
    assert error and "Could not fetch" in text


async def test_creates_and_closes_its_own_client() -> None:
    tool = HttpFetchTool(resolver=fake_resolver)
    text, error = await fetch(tool, "http://127.0.0.1/")  # blocked before any network use
    assert error and "private" in text


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("8.8.8.8", True),
        ("192.168.1.1", False),
        ("::ffff:127.0.0.1", False),
        ("2001:4860:4860::8888", True),
        ("fe80::1%eth0", False),
        ("224.0.0.1", False),
    ],
)
def test_is_public_address(address: str, public: bool) -> None:
    assert is_public_address(address) is public


def test_html_to_text_skips_scripts_and_styles() -> None:
    assert html_to_text("<p>a</p><script>x()</script><style>.c{}</style><div>b</div>") == "a\nb"


async def test_system_resolver_resolves_localhost() -> None:
    from agentforge.tools.builtin.http import system_resolver

    addresses = await system_resolver("localhost")
    assert addresses
    assert not any(is_public_address(a) for a in addresses)
