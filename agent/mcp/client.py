"""MCP Client - connects to MCP servers and registers their tools."""
from __future__ import annotations
import json
import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from pydantic import BaseModel, Field, create_model

from .protocol import (
    JsonRpcRequest, McpPingRequest, McpInitializeRequest,
    McpToolsListResponse, McpToolInfo, ContentUnion, ToolResult,
)
from .transports import StdioMcpTransport, SseMcpTransport
from ..tools import Tool, ToolContext, ToolRegistry, ToolCall, ToolResult as KenToolResult


@dataclass
class McpServerConfig:
    """Configuration for an MCP server connection."""
    # For stdio: command + args; for SSE: url
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    name: str | None = None  # Server display name
    
    @property
    def is_stdio(self) -> bool:
        return self.command is not None
    
    @property
    def server_name(self) -> str:
        return self.name or self.command.split("/")[-1] if self.command else "unnamed"


class DynamicTool(Tool):
    """A tool dynamically created from an MCP server's tool definition."""
    
    def __init__(self, info: McpToolInfo, transport: McpServerTransport, server_name: str):
        self.name = f"{server_name}:{info.name}"
        self.description = info.description or ""
        
        # Build Pydantic model from MCP schema
        schema = info.inputSchema
        if schema:
            self.input_model = _build_pydantic_model(info.name, schema)
        else:
            # No schema means no params required
            self.input_model = type(f"Mcp_{info.name}_Args", (BaseModel,), {
                "model_config": {"extra": "forbid"}
            })
            
        self._transport = transport
        self._tool_name = info.name
        
    def _extract_args(self, args_dict: dict) -> dict:
        """Extract just the JSON Schema parameters from the args dict."""
        if not args_dict:
            return {}
        result = {}
        # The args might be a flat dict matching the schema properties
        if isinstance(args_dict, dict):
            return args_dict
        return result
        
    async def call(self, args: BaseModel, ctx: ToolContext) -> KenToolResult:
        try:
            args_dict = args.model_dump()
            result = await self._transport.send_request(JsonRpcRequest(
                method="tools/call",
                params={"name": self._tool_name, "arguments": args_dict}
            ))
            
            content = result.get("content", [])
            error = result.get("isError", False)
            
            output_parts = []
            for item in content:
                item_type = item.get("type", "")
                if item_type == "text":
                    output_parts.append(item.get("text", ""))
                elif item_type == "image":
                    output_parts.append(f"[Image: {item.get('mimeType', 'unknown')}]")
                else:
                    output_parts.append(str(item))
                    
            output = "\n\n".join(output_parts).strip()
            if not output:
                output = "(empty response)"
                
            return KenToolResult(
                call_id=getattr(ctx, '_last_call_id', ''),
                output=output,
                is_error=error
            )
        except Exception as e:
            return KenToolResult(
                call_id=getattr(ctx, '_last_call_id', ''),
                output=f"MCP tool error ({type(e).__name__}): {e}",
                is_error=True
            )
    
    def is_concurrency_safe(self, args: BaseModel) -> bool:
        """Mark all MCP tools as safe by default - they can be batched."""
        return True


def _build_pydantic_model(tool_name: str, schema: Any) -> type[BaseModel]:
    """Build a Pydantic model from an MCP JSON Schema."""
    props = {}
    required_fields = []
    
    if hasattr(schema, 'properties') and schema.properties:
        for prop_name, prop_schema in schema.properties.items():
            field_type = _infer_pytype(prop_schema)
            field_desc = prop_schema.description
            field_default = Field(default=prop_schema.default, description=field_desc) if prop_schema.default is not None else Field(description=field_desc) if field_desc else Field()
            props[prop_name] = (field_type, field_default)
            
    if hasattr(schema, 'required') and schema.required:
        required_fields = schema.required
        
    model = type(
        f"Mcp_{tool_name}_Args", 
        (BaseModel,), 
        {
            **props,
            "model_config": {"extra": "forbid", "title": tool_name}
        }
    )
    
    if required_fields:
        # Create proper required fields
        new_props = {}
        for fname, (ftype, finfo) in props.items():
            if fname in required_fields:
                new_props[fname] = (ftype, Field(..., description=finfo.description if hasattr(finfo, 'description') else ''))
            else:
                new_props[fname] = (ftype, finfo)
        model = type(f"Mcp_{tool_name}_Args", (BaseModel,), {
            **new_props,
            "model_config": {"extra": "forbid", "title": tool_name}
        })
        
    return model


def _infer_pytype(schema: Any) -> type:
    """Infer Python type from MCP schema."""
    type_str = schema.type
    if not type_str:
        return str  # Default fallback
        
    mapping = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }
    return mapping.get(type_str, str)


class McpClient:
    """Manages connections to MCP servers and exposes their tools."""
    
    def __init__(self):
        self._connections: dict[str, McpServerTransport] = {}
        self._discovered_tools: list[tuple[McpToolInfo, McpServerTransport, str]] = []
        self._initialized = False
        
    async def connect_stdio(self, config: McpServerConfig) -> None:
        """Connect to an MCP server via stdio subprocess."""
        transport = StdioMcpTransport(
            command=config.command,
            args=config.args,
            env=config.env
        )
        
        # Initialize subprocess
        await transport.ensure_init()
        
        # MCP Handshake
        init_req = McpInitializeRequest()
        try:
            await transport.send_request(init_req)
        except Exception as e:
            await transport.close()
            raise ConnectionError(f"MCP initialization failed: {e}")
        
        # Verify initialized
        ping_req = McpPingRequest()
        try:
            await transport.send_request(ping_req)
        except Exception:
            # Ping is optional, continue anyway
            pass
            
        # Discover tools
        tools_resp = await transport.send_request(JsonRpcRequest(
            method="tools/list",
            params={}
        ))
        
        if "tools" in tools_resp:
            tools_list = tools_resp["tools"]
            server_name = config.server_name
            self._connections[config.server_name] = transport
            
            for tool_info in tools_list:
                dynamic_tool = DynamicTool(tool_info, transport, server_name)
                self._discovered_tools.append((tool_info, transport, server_name))
                
    async def connect_sse(self, config: McpServerConfig) -> None:
        """Connect to an MCP server via SSE."""
        if not config.url:
            raise ValueError("SSE connection requires a URL")
            
        transport = SseMcpTransport(config.url)
        
        # Initialize
        await transport.ensure_init()
        
        # MCP Handshake
        init_req = McpInitializeRequest()
        await transport.send_request(init_req)
        
        # Discover tools
        tools_resp = await transport.send_request(JsonRpcRequest(
            method="tools/list",
            params={}
        ))
        
        if "tools" in tools_resp:
            tools_list = tools_resp["tools"]
            server_name = config.server_name or config.url.split("//")[-1].split("/")[0]
            self._connections[config.url] = transport
            
            for tool_info in tools_list:
                dynamic_tool = DynamicTool(tool_info, transport, server_name)
                self._discovered_tools.append((tool_info, transport, server_name))
                
    async def close(self) -> None:
        """Close all connections."""
        for transport in self._connections.values():
            try:
                await transport.close()
            except Exception:
                pass
                
    def get_tools(self) -> list[DynamicTool]:
        """Get all discovered tools."""
        tools = []
        for tool_info, transport, server_name in self._discovered_tools:
            tools.append(DynamicTool(tool_info, transport, server_name))
        return tools
        
    def register_with_registry(self, registry: ToolRegistry) -> None:
        """Register all discovered tools with a Ken ToolRegistry."""
        for tool_info, transport, server_name in self._discovered_tools:
            dynamic_tool = DynamicTool(tool_info, transport, server_name)
            registry.register(dynamic_tool)
