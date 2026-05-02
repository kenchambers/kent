# MT5 Multi MCP Server — Setup & Usage

This document is a self-contained **LLM prompt** for setting up, connecting to,
and using the MT5 Multi MCP Server. Hand it to any code-LLM (Claude, Cursor,
Copilot, etc.) with shell access and it should be able to deploy and verify the
server end-to-end on a fresh Windows host.

---

## Prompt — begin

You are setting up the **MT5 Multi MCP Server**: a FastMCP-based HTTP server
that exposes MetaTrader 5 operations (account info, positions, orders, market
data, risk calcs, exit-reason classification) via JSON-RPC for LLM agents and
analysis scripts to consume.

### Architecture (read first)

- The server runs on **Windows** because the `MetaTrader5` Python package only
  works on Windows. WSL/Linux clients connect across the WSL→Windows host bridge.
- Each prop account corresponds to a separate MT5 terminal install on the
  Windows host (different login + server + binary path). The server connects to
  all of them via the `MetaTrader5` package and aggregates them under named
  `account_name` keys.
- Transport is HTTP on port 8000 with two access patterns:
  1. **Official MCP/SSE** — session-based, JSON-RPC over Server-Sent Events.
     Requires MCP-aware client.
  2. **Direct JSON-RPC POST** — simpler, what most analysis scripts use. Works
     without the `mcp` Python package.
- Important: These are **READ-ONLY monitoring terminals**. Actual EAs run on a
  separate hidden execution server. "Algo Trading: OFF" in the terminal UI is
  **normal and expected**.

### File Layout

| Path | What |
|---|---|
| `C:\Users\Administrator\mt5_multi_mcp\` (Windows) | server source repo |
| `…\server.py` | FastMCP entry point — ~31 tools registered via `@mcp.tool` |
| `…\mt5_manager.py` | manages connections across the multiple MT5 terminals |
| `…\mt5_accounts.json` | per-account config: `login`, `password`, `server`, `path` to `terminal64.exe`, `timeout` |
| `…\mt5_accounts.json.example` | sample with no secrets — copy to `mt5_accounts.json` and fill in |
| `…\run_http_server.bat` | Windows startup script |
| `…\setup_windows_vps.bat` | first-time Windows VPS setup (firewall, deps, scripts) |
| `…\requirements.txt` | Python deps (FastMCP 2.11+, MetaTrader5, fastapi, etc.) |
| `…\pyproject.toml` | uv-managed deps |
| `…\excursion_calculator.py`, `mae_calculator.py` | MAE/MFE math behind `calculate_excursion_analysis` |
| `…\exit_classifier.py` | PR #8: `classify_exit_reasons` 6-fingerprint logic |
| `…\equity_api.py` | balance/equity wrappers |
| `…\server.err.log`, `server.out.log` | server logs (Windows-side) |
| WSL: `/mnt/c/Users/Administrator/Documents/Github/mt5_multi_mcp/` | same repo viewed from WSL |
| WSL: `/home/administrator/.openclaw/workspace/scripts/mt5_http_client.py` | direct-HTTP client used by analysis scripts |
| WSL: `/home/administrator/.openclaw/workspace/scripts/mt5_mcp_client.py` | SSE/MCP-protocol client (uses `mcp` package) |
| WSL: `/home/administrator/.openclaw/workspace/skills/mt5-mcp/SKILL.md` | usage docs (this is the source-of-truth tool list) |

### Endpoint

- From Windows: `http://localhost:8000/mcp`
- From WSL: `http://172.29.0.1:8000/mcp` (or whichever IP `cat /etc/resolv.conf | grep nameserver` returns — that's the Windows host from WSL)
- Health check (no auth needed): `curl http://172.29.0.1:8000/mcp` — should
  return a `405 Method Not Allowed` if the server is up but refuses GET
  (expected; only POST works for MCP).

### Prerequisites (verify before doing anything)

1. **Windows host** with admin access. WSL clients are optional — server is
   what matters.
2. **Python 3.12+** installed at
   `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`
   (or wherever `python --version` resolves).
3. **`uv`** package manager: `curl -LsSf https://astral.sh/uv/install.sh | sh`
   on WSL/macOS, or PowerShell installer on Windows.
4. **MetaTrader5 terminals** already installed and configured. Each account
   needs its own MT5 install at a unique path with its own login/server.
5. Network: port `8000` reachable from clients (firewall rule on Windows for
   inbound 8000 from WSL/LAN).

### Step-by-Step Deployment

1. **Clone the repo** to `C:\Users\Administrator\mt5_multi_mcp\` (or pull
   latest if it exists).
   ```bash
   git clone https://github.com/kenchambers/mt5_multi_mcp.git C:\Users\Administrator\mt5_multi_mcp
   cd C:\Users\Administrator\mt5_multi_mcp
   ```

2. **Install dependencies via `uv`**:
   ```bash
   cd C:\Users\Administrator\mt5_multi_mcp
   uv sync
   ```
   If `uv sync` is unavailable, fall back to:
   `pip install -r requirements.txt` then explicitly
   `pip install MetaTrader5 fastmcp`.

3. **Configure accounts.** Copy `mt5_accounts.json.example` →
   `mt5_accounts.json` and fill in:
   ```json
   {
     "accounts": {
       "<account_name>": {
         "login": <int_login>,
         "password": "<broker_password>",
         "server": "<broker_server_name>",
         "path": "C:\\MT5_Accounts\\<account_dir>\\terminal64.exe",
         "timeout": 60000
       }
     }
   }
   ```
   `account_name` is what clients use as the `account_name` argument to all
   tools. `path` must be the absolute Windows path to the per-account
   `terminal64.exe`. **Do not commit this file** — it contains broker
   passwords.

4. **Configure Windows Firewall** (one-time, requires admin): allow inbound
   TCP on port 8000 from local network. The script `setup_windows_vps.bat`
   will do this if run as Administrator.

5. **Start the server.** Two options:
   - **Foreground (for testing):**
     ```cmd
     cd C:\Users\Administrator\mt5_multi_mcp
     uv run fastmcp run server.py:mcp --transport http --port 8000 --host 0.0.0.0
     ```
   - **Background via the bat:**
     ```cmd
     run_http_server.bat
     ```
   Server will print `Uvicorn running on http://0.0.0.0:8000` when ready.
   Each MT5 terminal also gets initialized and you'll see per-account
   `connected as <login>` lines.

6. **Verify the server is running:**
   ```bash
   # From WSL
   curl -i -X POST http://172.29.0.1:8000/mcp \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"setup-check","version":"0"}}}'
   ```
   Expected: `200 OK` with an SSE response containing
   `"serverInfo":{"name":"MT5 Multi MCP Server"...}` and a `mcp-session-id`
   header — capture that header value, you'll reuse it.

7. **List available tools** (using the saved session id):
   ```bash
   curl -X POST http://172.29.0.1:8000/mcp \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -H "mcp-session-id: <SESSION_ID_FROM_STEP_6>" \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
   ```
   Or use the helper:
   `python3 /home/administrator/.openclaw/workspace/scripts/mt5_http_client.py list-tools`

8. **Smoke-test one tool:**
   ```bash
   python3 scripts/mt5_http_client.py call list_mt5_accounts '{}'
   python3 scripts/mt5_http_client.py call get_mt5_account_info '{"account_name":"<one_account_name>"}'
   ```
   If these return real data, server is healthy.

### Tool Catalog (~31 tools, grouped by purpose)

**Account / state**
- `list_mt5_accounts` — list all configured accounts (no args)
- `get_mt5_account_info` — full account info: balance, equity, margin,
  leverage, profit, etc. Args: `{"account_name": "..."}`
- `get_balance_equity` — quick balance + equity snapshot.
  Args: `{"account_name": "..."}`
- `list_positions` — open positions with running P/L.
  Args: `{"account_name": "..."}`
- `list_orders` — pending orders. Args: `{"account_name": "..."}`

**Trading (do not call these in monitoring contexts)**
- `open_position`, `close_position`, `modify_position`, `place_order` — write
  operations. Don't issue from analysis scripts; the MT5 terminals are
  read-only monitoring instances by design.

**Market data**
- `get_chart_data` — OHLCV candles for technical analysis.
  Args: `{"account_name", "symbol", "timeframe", "count"}`
- `get_symbol_info` — symbol details (spread, digits, tick size).
  Args: `{"account_name", "symbol"}`

**History + analysis**
- `get_mt5_history_deals` — closed deal history.
  Args: `{"account_name", "date_from", "date_to"}` (ISO timestamps)
- `calculate_excursion_analysis` — MAE/MFE per position.
  Args: `{"account_name", "position_id"}` or batched
- `classify_exit_reasons` — fingerprints exits as one of `basket_TP`,
  `equity_stop`, `session_end`, `max_minutes`, `trailing_stop`,
  `ea_unclassified`. Args: `{"account_name", "magic", "date_from", "date_to"}`

**Risk**
- `calculate_position_size` — lot size from risk%.
  Args: `{"account_balance", "risk_percentage", "stop_loss_pips", "pip_value"}`

**Sanity**
- `greet` — connectivity test. Args: `{"name": "..."}`

For the canonical schema of each tool's args + return shape, run `tools/list`
against the live server — it returns full JSON Schema for every tool.

### How Clients Use It

**From any code-LLM with shell access:**
```bash
# All five GFT accounts at once
for acct in ftmo_10k ftmo_100k gft_100k_live gft_100k_phase1_942 gft_100k_phase1_743 gft_100k_phase1_613; do
  python3 /home/administrator/.openclaw/workspace/scripts/mt5_http_client.py \
    call get_mt5_account_info "{\"account_name\":\"$acct\"}"
done
```

**From Python (no extra deps):**
```python
import requests, json
URL = "http://172.29.0.1:8000/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

# 1. initialize → capture session id
r = requests.post(URL, json={
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "my-client", "version": "0"},
    },
}, headers=HEADERS)
SID = r.headers["mcp-session-id"]

# 2. call any tool
def call(name, args):
    body = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    }
    r = requests.post(URL, json=body, headers={**HEADERS, "mcp-session-id": SID})
    # Parse SSE — extract data: line, then JSON
    for line in r.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None

print(call("get_balance_equity", {"account_name": "ftmo_10k"}))
```

**From Claude Code with `.mcp.json` registration:**
Add to project `.mcp.json`:
```json
{
  "mcpServers": {
    "mt5": {
      "url": "http://172.29.0.1:8000/mcp",
      "transport": "sse"
    }
  }
}
```
Then Claude Code surfaces the tools natively as `mcp__mt5__<tool_name>`.

### Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `Failed to connect to 172.29.0.1:8000` | server not running on Windows | start it: `run_http_server.bat`, or check `Get-Process python` on Windows |
| `MetaTrader5 module not found` in server logs | Python package missing on the Windows host | on Windows: `python -m pip install MetaTrader5` (NOT in WSL — server runs on Windows) |
| Tools list works but `get_mt5_account_info` returns empty | terminal at the configured `path` isn't running, or login failed | open the MT5 terminal manually, log in, leave it running. The server attaches to existing terminal sessions. |
| `Session expired` errors | session id stale (server restarted) | re-run `initialize`, capture new session id |
| `Algo Trading: OFF` on terminal UI | **expected** — these terminals are monitoring-only by design | leave it OFF; ignore |
| Server crashes on startup | bad `mt5_accounts.json` | run `python -m json.tool mt5_accounts.json` to validate; check each `path` exists |
| WSL can't reach 172.29.0.1 | wrong host IP for this WSL setup | run `cat /etc/resolv.conf \| grep nameserver` from WSL — that's the right IP |

### Operational Notes

- Server logs go to `server.out.log` and `server.err.log` in the repo dir.
  Tail them when debugging.
- The `mt5_manager.py` lazy-connects to terminals on first tool call per
  account, then caches the connection. First call to a cold account is slow
  (~2–3s); subsequent calls are sub-100ms.
- Restart the server after editing `mt5_accounts.json`.
- Strategy tester / backtest mode is **not** supported — these are live
  terminal connections only.

## Prompt — end
