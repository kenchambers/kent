# MT5 Multi MCP Server

FastMCP-based HTTP server exposing MetaTrader 5 operations to AI agents and analysis scripts.

## Quick Start

Connect via HTTP SSE at `http://172.29.0.1:8000/mcp` (WSL → Windows bridge).

### Python (no extra deps)
```python
import requests, json

URL = "http://172.29.0.1:8000/mcp"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def init():
    r = requests.post(URL, json={
        "jsonrpc":"2.0","id":1,"method":"initialize",
        "params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"0"}}
    }, headers=HEADERS)
    return r.headers["mcp-session-id"]

SESSION_ID = init()

def call_tool(name, args):
    body = {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}}
    r = requests.post(URL, json=body, headers={**HEADERS, "mcp-session-id": SESSION_ID})
    for line in r.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None

print(call_tool("list_mt5_accounts", {}))
```

### Claude Code
Add to `.mcp.json`:
```json
{"mcpServers":{"mt5":{"url":"http://172.29.0.1:8000/mcp","transport":"sse"}}}
```

### Shell Helper
```bash
python3 /home/administrator/.openclaw/workspace/scripts/mt5_http_client.py call list_mt5_accounts '{}'
```

Find correct WSL→Windows IP: `cat /etc/resolv.conf | grep nameserver`

## Tool Inventory (~31 tools)

| Category | Tools |
|---|---|
| Account state | `list_mt5_accounts`, `get_mt5_account_info`, `get_balance_equity`, `test_mt5_connection` |
| Positions/orders | `list_positions`, `list_orders`, `get_mt5_positions_total`, `get_mt5_orders_total` |
| Market data | `get_chart_data`, `get_mt5_ohlcv`, `get_symbol_info` |
| History/analysis | `get_mt5_history_deals`, `get_mt5_history_orders`, `calculate_excursion_analysis`, `classify_exit_reasons` |
| Risk management | `calculate_position_size` |
| Utility | `greet`, `get_server_status`, `list_mt5_windows` |

All account-targeting tools take `"account_name"` string arg.

## Operational Notes

- **Read-only terminals**: Trading tools exist but MT5 instances are monitoring-only by design
- **Session expiry**: Re-run `initialize()` to get new session ID
- **First call slow**: Accounts lazy-connect (~2-3s); subsequent calls sub-100ms
- **Config**: `mt5_accounts.json` in `/mnt/c/Users/Administrator/Documents/Github/mt5_multi_mcp/`
- **Server logs**: `server.out.log`, `server.err.log` on Windows host
- **Troubleshooting**: See `SKILL.md` for full table

## Source

Server source: https://github.com/kenchambers/mt5_multi_mcp
