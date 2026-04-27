"""
DuckDuckGo HTML web search — no API key required.

Scrapes https://html.duckduckgo.com/html/ and parses result anchors. This is
the same approach a browser would use; no third-party search API is involved.
DDG may rate-limit or change markup; treat as best-effort.
"""
from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import httpx
from pydantic import BaseModel, Field
from selectolax.parser import HTMLParser

from ..events import ToolResult
from ..tools import ToolContext

DDG_HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SECONDS = 15.0
MAX_RESULTS_HARD_CAP = 25


class WebSearchArgs(BaseModel):
    query: str = Field(min_length=2, description="The search query")
    max_results: int = Field(
        default=10,
        ge=1,
        le=MAX_RESULTS_HARD_CAP,
        description="Max results to return (1-25)",
    )


def _unwrap_ddg_redirect(href: str) -> str:
    """DDG wraps result links as /l/?uddg=<encoded-url>. Unwrap to the real URL."""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
    except ValueError:
        return href
    if parsed.path == "/l/" and parsed.query:
        params = parse_qs(parsed.query)
        if "uddg" in params and params["uddg"]:
            return unquote(params["uddg"][0])
    return href


def _parse_results(html: str, limit: int) -> list[dict]:
    tree = HTMLParser(html)
    results: list[dict] = []
    for node in tree.css("div.result"):
        title_node = node.css_first("a.result__a")
        snippet_node = node.css_first("a.result__snippet") or node.css_first(
            ".result__snippet"
        )
        if title_node is None:
            continue
        href = title_node.attributes.get("href", "")
        if not href:
            continue
        url = _unwrap_ddg_redirect(href)
        title = title_node.text(strip=True)
        snippet = snippet_node.text(strip=True) if snippet_node is not None else ""
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


class WebSearch:
    name = "web_search"
    description = (
        "Search the web via DuckDuckGo (no API key required). Returns a list of "
        "{title, url, snippet} hits. Use web_fetch on a returned URL to read the "
        "full page contents."
    )
    input_model = WebSearchArgs

    def is_concurrency_safe(self, args: WebSearchArgs) -> bool:
        return True

    async def call(self, args: WebSearchArgs, ctx: ToolContext) -> ToolResult:
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"User-Agent": DEFAULT_USER_AGENT},
                follow_redirects=True,
            ) as client:
                response = await client.post(
                    DDG_HTML_ENDPOINT,
                    data={"q": args.query},
                )
                response.raise_for_status()
                html = response.text
        except httpx.HTTPError as e:
            return ToolResult(
                call_id="",
                output=f"DuckDuckGo request failed: {e}",
                is_error=True,
            )

        hits = _parse_results(html, args.max_results)
        if not hits:
            return ToolResult(
                call_id="",
                output=(
                    f'No results parsed for query "{args.query}". DuckDuckGo may '
                    "be rate-limiting or its HTML markup may have changed."
                ),
            )

        return ToolResult(
            call_id="",
            output={"query": args.query, "results": hits},
        )
