"""
Fetch a URL and return its main contents as markdown.

Mirrors the safety knobs from claude-code/tools/WebFetchTool/utils.ts:
- URL length cap, public-host check
- HTTP content-length cap (10 MB)
- Request timeout
- HTML → markdown via markdownify
- Output truncated to MAX_MARKDOWN_LENGTH

Unlike claude-code's version, summarization is left to the calling LLM —
the tool just returns the markdown.
"""
from __future__ import annotations

from urllib.parse import urlparse

import httpx
from markdownify import markdownify
from pydantic import BaseModel, Field

from ..events import ToolResult
from ..tools import ToolContext

MAX_URL_LENGTH = 2000
MAX_HTTP_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB
FETCH_TIMEOUT_SECONDS = 30.0
MAX_MARKDOWN_LENGTH = 100_000  # chars
DEFAULT_USER_AGENT = (
    "agent/0.1 (+https://github.com/) httpx web_fetch tool"
)


class WebFetchArgs(BaseModel):
    url: str = Field(description="The URL to fetch (http or https)")
    max_chars: int = Field(
        default=MAX_MARKDOWN_LENGTH,
        ge=500,
        le=MAX_MARKDOWN_LENGTH,
        description="Truncate the returned markdown to this many characters",
    )


def _validate_url(url: str) -> str | None:
    """Return error message if invalid, None if OK."""
    if len(url) > MAX_URL_LENGTH:
        return f"URL too long (max {MAX_URL_LENGTH} chars)"
    try:
        parsed = urlparse(url)
    except ValueError:
        return "Malformed URL"
    if parsed.scheme not in ("http", "https"):
        return "URL must use http or https"
    if parsed.username or parsed.password:
        return "URLs with credentials are not allowed"
    if not parsed.hostname or "." not in parsed.hostname:
        return "Hostname must be publicly resolvable (contain a dot)"
    return None


def _to_markdown(content_type: str, body: str) -> str:
    if "html" in content_type.lower():
        return markdownify(body, heading_style="ATX")
    return body


class WebFetch:
    name = "web_fetch"
    description = (
        "Fetch a URL and return its contents as markdown (HTML pages are "
        "converted; plain-text/markdown pages are returned as-is). Truncates "
        f"to {MAX_MARKDOWN_LENGTH:,} characters by default. Use after web_search "
        "to read a specific result."
    )
    input_model = WebFetchArgs

    def is_concurrency_safe(self, args: WebFetchArgs) -> bool:
        return True

    async def call(self, args: WebFetchArgs, ctx: ToolContext) -> ToolResult:
        err = _validate_url(args.url)
        if err is not None:
            return ToolResult(call_id="", output=err, is_error=True)

        try:
            async with httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "text/markdown, text/html, text/plain, */*",
                },
                follow_redirects=True,
                max_redirects=10,
            ) as client:
                response = await client.get(args.url)
        except httpx.HTTPError as e:
            return ToolResult(
                call_id="",
                output=f"Fetch failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        if response.status_code >= 400:
            return ToolResult(
                call_id="",
                output=f"HTTP {response.status_code} {response.reason_phrase} for {args.url}",
                is_error=True,
            )

        content_length = int(response.headers.get("content-length") or 0)
        if content_length and content_length > MAX_HTTP_CONTENT_LENGTH:
            return ToolResult(
                call_id="",
                output=(
                    f"Response too large: {content_length:,} bytes "
                    f"(limit {MAX_HTTP_CONTENT_LENGTH:,})"
                ),
                is_error=True,
            )
        if len(response.content) > MAX_HTTP_CONTENT_LENGTH:
            return ToolResult(
                call_id="",
                output=(
                    f"Response body exceeded limit "
                    f"({len(response.content):,} > {MAX_HTTP_CONTENT_LENGTH:,} bytes)"
                ),
                is_error=True,
            )

        content_type = response.headers.get("content-type", "")
        markdown = _to_markdown(content_type, response.text)
        truncated = False
        if len(markdown) > args.max_chars:
            markdown = markdown[: args.max_chars]
            truncated = True

        return ToolResult(
            call_id="",
            output={
                "url": str(response.url),
                "status": response.status_code,
                "content_type": content_type,
                "truncated": truncated,
                "markdown": markdown,
            },
        )
