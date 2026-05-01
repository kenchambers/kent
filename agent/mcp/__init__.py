"""MCP Client integration for Ken.

Connects to external MCP servers and exposes their tools as native Ken tools.
Supports both stdio subprocesses and SSE remote connections.
"""

from .client import McpClient, McpServerConfig, DynamicTool
from .protocol import JsonRpcRequest, McpPingRequest, McpInitializeRequest
from .transports import StdioMcpTransport, SseMcpTransport

__all__ = [
    "McpClient",
    "McpServerConfig", 
    "DynamicTool",
    "StdioMcpTransport",
    "SseMcpTransport",
    "JsonRpcRequest",
    "McpPingRequest",
    "McpInitializeRequest",
]
