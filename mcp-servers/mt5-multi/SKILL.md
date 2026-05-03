---
name: mt5-multi-mcp
description: Connect to and operate MetaTrader 5 via HTTP-MCP server for trading account monitoring, analysis, and risk calculations across multiple prop firm accounts.
---

# MT5 Multi MCP Server

Connects to the local MT5 Multi MCP server running on Windows host (`http://172.29.0.1:8000`). Provides ~30 tools for multi-account monitoring, market data, historical analysis, and risk calculations.

## Connection (WSL → Windows)

Server runs at `http://172.29.0.1:8000/mcp`. Verify: `curl -s http://172.29.0.1:8000/mcp` → 406 expected.

Two ways to interact:

### 1. Direct HTTP (no deps beyond `requests`)
```python
import requests, json

URL = "http://172.29.0.1:8000/mcp"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def init():
    r = requests.post(URL, json={"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"0"}}}, headers=HEADERS)
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

### 2. Claude Code `.mcp.json`
Add to project root `.mcp.json`:
```json
{"mcpServers":{"mt5":{"url":"http://172.29.0.1:8000/mcp","transport":"sse"}}}
```

### 3. Shell helper script
```bash
python3 /home/administrator/.openclaw/workspace/scripts/mt5_http_client.py call list_mt5_accounts '{}'
python3 /home/administrator/.openclaw/workspace/scripts/mt5_http_client.py call get_mt5_account_info '{"account_name":"ftmo_10k"}'
```

To find correct IP: `cat /etc/resolv.conf | grep nameserver`

## Tool Catalog

Grouped by purpose. All tools that target an account take `"account_name"` string arg.

**Account state**
| Tool | Args | Returns |
|---|---|---|
| `list_mt5_accounts` | none | list of configured account aliases |
| `get_mt5_account_info` | `account_name` | balance, equity, margin, leverage, profit, free_margin |
| `get_balance_equity` | `account_name` | quick {balance, equity} snapshot |
| `test_mt5_connection` | `account_name` | connectivity status |

**Positions & orders**
| Tool | Args | Returns |
|---|---|---|
| `list_positions` | `account_name` | open positions with P/L |
| `list_orders` | `account_name` | pending orders |
| `get_mt5_positions_total` | `account_name` | count of open positions |
| `get_mt5_orders_total` | `account_name` | count of pending orders |

**Market data**
| Tool | Args | Returns |
|---|---|---|
| `get_chart_data` | `account_name`, `symbol`, `timeframe`, `count` | OHLCV candles |
| `get_mt5_ohlcv` | `account_name`, `symbol`, `timeframe`, `count` | candle data |
| `get_symbol_info` | `account_name`, `symbol` | spread, digits, tick_size |

**History + analysis**
| Tool | Args | Returns |
|---|---|---|
| `get_mt5_history_deals` | `account_name`, `date_from`, `date_to` (ISO) | closed deal history |
| `get_mt5_history_orders` | `account_name`, `date_from`, `date_to` | order history |
| `calculate_excursion_analysis` | `account_name`, `position_id` or batch | MFE/MAE per trade |
| `classify_exit_reasons` | `account_name`, `magic`, `date_from`, `date_to` | exit classification (basket_TP, equity_stop, trailing_stop, etc.) |

**Risk management**
| Tool | Args | Returns |
|---|---|---|
| `calculate_position_size` | `account_balance`, `risk_percentage`, `stop_loss_pips`, `pip_value` | position_size, risk_amount |

**Utility**
| Tool | Args | Returns |
|---|---|---|
| `greet` | `name` | connectivity test |
| `get_server_status` | none | server health info |
| `list_mt5_windows` | none | MT5 terminal windows |

## Workflow Patterns

**Check all accounts quickly:**
```bash
for acct in ftmo_10k gft_100k_live gft_100k_phase1_942; do
  python3 scripts/mt5_http_client.py call get_mt5_account_info "{\"account_name\":\"$acct\"}"
done
```

**Analyze past performance:**
1. `get_mt5_history_deals` with date range
2. `calculate_excursion_analysis` for MAE/MFE per trade
3. `classify_exit_reasons` to understand exit patterns

## Operational Notes

- **Read-only terminals**: Trading tools exist (`open_position`, etc.) but these MT5 instances are monitoring-only. Don't write-trade through them.
- **Session expiry**: If "session expired", re-run `initialize()` to get new `mcp-session-id`.
- **First call slow**: Accounts lazy-connect (~2-3s). Subsequent calls sub-100ms.
- **Config**: Account credentials in `mt5_accounts.json` inside the `mt5_multi_mcp` repo at `C:\Users\Administrator\Documents\Github\mt5_multi_mcp\`.
- **Server logs**: `server.out.log`, `server.err.log` on Windows host.
- **Troubleshooting**:
  - Connection fail → check server running (`run_http_server.bat`)
  - Empty account info → MT5 terminal not logged in
  - Wrong IP → verify with `cat /etc/resolv.conf | grep nameserver`
