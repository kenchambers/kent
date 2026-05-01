"""MCP (Model Context Protocol) JSON-RPC message types."""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Literal
from pydantic import BaseModel


# --- JSON-RPC 2.0 primitives ---

@dataclass
class JsonRpcRequest:
    jsonrpc: str = "2.0"
    method: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    id: int | str = 1

    def to_dict(self) -> dict:
        d = {"jsonrpc": self.jsonrpc, "method": self.method}
        if self.params:
            d["params"] = self.params
        if self.id is not None:
            d["id"] = self.id
        return d

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict()).encode("utf-8") + b"\n"

    @classmethod
    def from_response(cls, resp: str) -> dict[str, Any]:
        """Parse and extract fields from a JSON-RPC response string."""
        return json.loads(resp)


@dataclass
class McpServerTransport:
    """Base transport for connecting to an MCP server."""
    async def send_request(self, request: JsonRpcRequest) -> dict[str, Any]: ...
    async def close(self) -> None: ...


# --- MCP content types (for tool results) ---

TextContent = dict  # {"type": "text", "text": "..."}
ImageContent = dict  # {"type": "image", "data": "...", "mimeType": "..."}
ResourceContent = dict  # {"type": "resource", ...}

ContentUnion = TextContent | ImageContent | ResourceContent

ToolResult = dict  # {"content": list[ContentUnion], "isError": bool}


# --- MCP schemas for tools/list & tools/call responses ---

class McpSchema(BaseModel):
    """A sub-schema of a tool input schema."""
    type: str | None = None
    description: str | None = None
    properties: dict[str, "McpSchema"] | None = None
    required: list[str] | None = None
    default: Any = None
    enum: list | None = None
    format: str | None = None
    items: "McpSchema | None" = None


class McpToolInputSchema(McpSchema):
    """The `inputSchema` of an MCP tool — a JSON Schema subset."""
    pass


class McpToolArgument(BaseModel):
    name: str
    description: str | None = None
    schema: McpToolInputSchema | None = None
    defaultValue: Any = None


class McpToolInfo(BaseModel):
    """Information about a tool offered by an MCP server."""
    name: str
    description: str | None = None
    inputSchema: McpToolInputSchema | None = None
    arguments: list[McpToolArgument] | None = None


class McpToolsListResponse(BaseModel):
    """Response to tools/list."""
    tools: list[McpToolInfo]
    _meta: dict[str, Any] | None = field(default=None, alias="_meta")


class McpPingRequest(JsonRpcRequest):
    """Ping — simple health check with no parameters."""
    def __init__(self):
        super().__init__(method="ping", id=0)


class McpInitializeRequest(JsonRpcRequest):
    """Handshake with version negotiation."""
    PROTOCOL_VERSION = "2024-11-05"

    def __init__(self):
        super().__init__(
            method="initialize",
            params={
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ken-mcp-client", "version": "0.1.0"},
            },
        )
