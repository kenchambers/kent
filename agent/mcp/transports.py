"""MCP SSE transport for remote MCP servers."""
from __future__ import annotations
import asyncio
import json
import re
from typing import Any
from urllib.parse import urljoin
import httpx

from .protocol import McpServerTransport, JsonRpcRequest


class SseMcpTransport(McpServerTransport):
    """Communicates with an MCP server via SSE (Server-Sent Events)."""

    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._sse_url: str | None = None
        self._message_endpoint: str | None = None
        self._request_id: int = 0
        self._response_waiters: dict[int | str, asyncio.Future[dict[str, Any]]] = {}
        self._sse_task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    async def _discover_endpoints(self) -> None:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(self.url)
            response.raise_for_status()
            
            body = response.text
            
            # Try to find SSE endpoint in HTML <link> tags
            sse_match = None
            
            # Pattern for <link rel="alternate" type="text/event-stream" href="...">
            link_pattern = re.compile(
                r'<link[^>]*rel=["\']alternate["\'][^>]*href=["\']([^"\']+)["\']'
                r'[^>]*type=["\']text/event-stream["\']',
                re.IGNORECASE
            )
            sse_match = link_pattern.search(body)
            
            if not sse_match:
                # Alternative pattern
                alt_pattern = re.compile(
                    r'<a[^>]*href=["\']([^"\']+)["\'][^>]*type=["\']text/event-stream["\']',
                    re.IGNORECASE
                )
                sse_match = alt_pattern.search(body)
            
            # Try JSON configuration
            if not sse_match:
                try:
                    data = json.loads(body)
                    found_url = data.get("sseEndpoint") or data.get("endpoint", "")
                    if found_url:
                        class Match:
                            def group(self, n):
                                return found_url
                        sse_match = Match()
                except (json.JSONDecodeError, ValueError):
                    pass
            
            if sse_match:
                sse_url = sse_match.group(1) if hasattr(sse_match, "group") else str(sse_match)
                if not sse_url.startswith("http"):
                    sse_url = urljoin(self.url, sse_url)
                self._sse_url = sse_url
            else:
                # Fallback: assume same URL has SSE
                self._sse_url = self.url
                
            self._message_endpoint = self.url

    async def send_request(self, request: JsonRpcRequest) -> dict[str, Any]:
        if self._message_endpoint is None:
            raise RuntimeError("Transport not initialized")
            
        self._request_id += 1
        request.id = self._request_id
        
        headers = {"Content-Type": "application/json"}
        
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self._message_endpoint,
                content=request.to_json(),
                headers=headers,
            )
            response.raise_for_status()
            
            result = response.json()
            
            if "error" in result:
                err_msg = result["error"].get("message", str(result["error"]))
                raise RuntimeError(err_msg)
                
            return result.get("result", {})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sse_task:
            self._sse_task.cancel()
        for fut in self._response_waiters.values():
            if not fut.done():
                fut.set_exception(RuntimeError("MCP server connection closed"))

    async def ensure_init(self) -> None:
        await self._discover_endpoints()
