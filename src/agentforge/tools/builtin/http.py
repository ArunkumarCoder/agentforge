"""http_fetch: read a web page as text, with SSRF protection.

A model that can fetch URLs could be tricked (for example by text on a page it
read) into calling internal services: ``http://localhost:8080/admin``, a cloud
metadata endpoint such as ``http://169.254.169.254/``, or a machine on the
office network. This tool blocks that:

- only ``http``/``https`` on standard ports,
- the host is resolved first and **every** address must be public (no
  loopback, private, link-local, multicast or reserved ranges),
- redirects are followed by hand, and each hop is checked again,
- optional host allow-list, response size cap and timeout,
- HTML is reduced to readable text.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx2
from pydantic import BaseModel, Field

from agentforge.tools.base import BaseTool, ToolContext, ToolError, ToolPermission

Resolver = Callable[[str], Awaitable[list[str]]]
MAX_REDIRECTS = 3
DEFAULT_MAX_BYTES = 300_000
USER_AGENT = "AgentForge/0.1 (+https://github.com/ArunkumarCoder/agentforge)"


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


class HttpFetchArgs(BaseModel):
    url: str = Field(description="Full http(s) URL to fetch")


class HttpFetchTool(BaseTool[HttpFetchArgs]):
    name = "http_fetch"
    description = (
        "Fetch a public web page or text resource and return its text content "
        "(HTML is converted to plain text). Internal and private addresses are blocked."
    )
    args_model = HttpFetchArgs
    permission = ToolPermission.NETWORK
    timeout_s = 30.0
    max_output_chars = 40_000

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str] | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        client: httpx2.AsyncClient | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        super().__init__()
        self.allowed_hosts = {h.lower() for h in allowed_hosts} if allowed_hosts else None
        self.max_bytes = max_bytes
        self._client = client
        self._resolver = resolver

    async def run(self, args: HttpFetchArgs, ctx: ToolContext) -> str:
        client = self._client or httpx2.AsyncClient(
            timeout=self.timeout_s - 2, follow_redirects=False, headers={"User-Agent": USER_AGENT}
        )
        try:
            url = args.url
            for _ in range(MAX_REDIRECTS + 1):
                await self._check_url(url)
                async with client.stream("GET", url) as response:
                    if response.is_redirect and "location" in response.headers:
                        url = urljoin(url, response.headers["location"])
                        continue
                    body = await self._read_limited(response)
                    if response.status_code >= 400:
                        raise ToolError(f"HTTP {response.status_code} from {url}")
                    return self._render(url, response.headers.get("content-type", ""), body)
            raise ToolError(f"Too many redirects (more than {MAX_REDIRECTS}).")
        except httpx2.HTTPError as exc:
            raise ToolError(f"Could not fetch {args.url}: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                await client.aclose()

    async def _check_url(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise ToolError("Only http and https URLs are allowed.")
        host = (parts.hostname or "").lower()
        if not host:
            raise ToolError(f"Invalid URL: '{url}'.")
        if parts.port not in (None, 80, 443):
            raise ToolError("Only the standard ports 80 and 443 are allowed.")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ToolError(f"Blocked: '{host}' is a private or internal host name.")
        if self.allowed_hosts is not None and host not in self.allowed_hosts:
            raise ToolError(f"Host '{host}' is not on the allow-list.")
        try:
            addresses = await self._resolver(host)
        except OSError as exc:
            raise ToolError(f"Could not resolve host '{host}'.") from exc
        if not addresses or not all(is_public_address(a) for a in addresses):
            raise ToolError(f"Blocked: '{host}' resolves to a private or internal address.")

    async def _read_limited(self, response: httpx2.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= self.max_bytes:
                break
        return b"".join(chunks)[: self.max_bytes]

    def _render(self, url: str, content_type: str, body: bytes) -> str:
        text = body.decode("utf-8", errors="replace")
        if "html" in content_type.lower() or text.lstrip()[:15].lower().startswith(
            ("<!doctype html", "<html")
        ):
            text = html_to_text(text)
        cut = f"\n...[cut at {self.max_bytes} bytes]" if len(body) >= self.max_bytes else ""
        return f"URL: {url}\n\n{text.strip()}{cut}"


class _TextExtractor(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "svg", "head", "template"})
    BREAK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)
