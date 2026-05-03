# MT5 MCP Server Setup — LLM Prompt

This file is a paste-ready prompt to instruct an LLM (Claude / Cursor / etc.) on
how to set up and connect to the MT5 Multi MCP server. Hand the receiving LLM
this file plus access to a Windows shell + the `mt5_multi_mcp` repo and it has
everything it needs to bring the server up and connect a client.

---

> **Task:** Set up and connect to the MT5 Multi MCP server. This is a FastMCP-based Python server exposing ~30 MetaTrader 5 tools (accounts, orders, positions, history, OHLCV, MAE/MFE excursion, exit classification) over stdio or HTTP transport.
>
> **Repository:** `C:\Users\Administrator\Documents\Github\mt5_multi_mcp` (Windows host). Already cloned — do not re-clone, just `git pull` if needed.
>
> **Prerequisites on the Windows host:**
> - Python 3.10+ installed via `uv` package manager
> - One MetaTrader 5 terminal binary per account at `C:\MT5_Accounts\<alias>\terminal64.exe`
> - Each MT5 terminal is logged in interactively at least once (so credentials are cached)
>
> **Step 1 — Configure accounts.**
> Create/edit `mt5_accounts.json` at the repo root:
> ```json
> {
>   "accounts": {
>     "<alias>": {
>       "login": 521005217,
>       "password": "<pwd>",
>       "server": "<broker-server-name>",
>       "path": "C:\\MT5_Accounts\\<alias>\\terminal64.exe",
>       "timeout": 60000
>     }
>   }
> }
> ```
> Server names must match the broker exactly (`FTMO-Server2`, `OANDA-Prop Trader`, `GoatFunded-Server`, etc.). Each alias needs its own terminal binary at `path`.
>
> **Step 2 — Configure environment.**
> Copy `.env.example` → `.env`. Set `MT5_CONFIG_PATH=mt5_accounts.json` (relative to repo root, or absolute). `NOVITA_API_KEY` and `SUPABASE_*` are optional (only used by `llm_validate_analysis` and `equity_api.py`).
>
> **Step 3 — Choose transport.**
>
> **Option A — stdio (single-client subprocess, e.g. Claude Desktop):**
> ```
> claude mcp add --transport stdio --scope project mt5-multi -- ^
>   uv run --directory "C:/Users/Administrator/Documents/Github/mt5_multi_mcp" python server.py
> ```
>
> **Option B — HTTP streamable-http (multi-client, network-accessible) — preferred for Claude Code from WSL:**
> ```
> cd C:\Users\Administrator\Documents\Github\mt5_multi_mcp
> .\run_http_server.bat 8000 0.0.0.0
> ```
> Or directly: `uv run python start_http.py` — runs `mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)`.
> Endpoint: `http://<windows-host-ip>:8000/mcp`.
>
> **Open Windows firewall once (admin shell):**
> ```
> .\configure_firewall.bat 8000
> ```
>
> **Step 4 — Connect a client.**
>
> **From Claude Code (HTTP), add to `.mcp.json` in your project root:**
> ```json
> {
>   "mcpServers": {
>     "mt5-multi": {
>       "url": "http://172.29.0.1:8000/mcp",
>       "transport": "streamable-http"
>     }
>   }
> }
> ```
> From WSL2, the Windows host is reachable at `172.29.0.1` (verify with `ip route show default | awk '{print $3}'`). On a remote VPS, use the public IP.
>
> **From Claude Desktop, edit `claude_desktop_config.json`:**
> ```json
> {
>   "mcpServers": {
>     "mt5-multi": {
>       "command": "uv",
>       "args": ["run", "--directory", "C:\\Users\\Administrator\\Documents\\Github\\mt5_multi_mcp", "python", "server.py"]
>     }
>   }
> }
> ```
>
> **From custom Python (using `mcp[sse]`):**
> ```python
> from mcp import ClientSession
> from mcp.client.sse import sse_client
> async with sse_client("http://172.29.0.1:8000/mcp") as (read, write):
>     async with ClientSession(read, write) as session:
>         await session.initialize()
>         result = await session.call_tool("get_mt5_account_info", {"account_name": "ftmo_10k"})
> ```
>
> **Step 5 — Verify.**
> 1. Call `list_mt5_accounts` → returns all aliases from `mt5_accounts.json`.
> 2. Call `test_mt5_connection` with `account_name=<alias>` → confirms login works.
> 3. Call `get_mt5_account_info` with `account_name=<alias>` → returns balance/equity/leverage.
>
> If any of those fails: (a) confirm the MT5 terminal at `path` is running and logged in, (b) confirm the firewall rule, (c) check the `run_http_server.bat` console window for tracebacks.
>
> **Tool inventory (~30 tools, callable by name from any MCP client):**
> - **Account mgmt (7):** `list_mt5_accounts`, `get_mt5_account_info`, `switch_mt5_account`, `get_current_mt5_account`, `test_mt5_connection`, `initialize_mt5`, `shutdown_mt5`
> - **Active orders/positions (4):** `get_mt5_orders_total`, `get_mt5_orders`, `get_mt5_positions_total`, `get_mt5_positions`
> - **Order calc (3):** `calculate_mt5_order_profit`, `check_mt5_order`, `calculate_position_size`
> - **History (4):** `get_mt5_history_orders_total`, `get_mt5_history_orders`, `get_mt5_history_deals_total`, `get_mt5_history_deals`
> - **Charts/OHLCV (2):** `get_mt5_chart_data`, `get_mt5_ohlcv`
> - **Forensic (5):** `calculate_excursion_analysis` (MFE/MAE per trade), `calculate_mae_for_positions`, `classify_exit_reasons`, `generate_verification_script`, `llm_validate_analysis`
> - **Misc:** `get_mt5_swap_summary`, `get_mt5_tradesviz_comparison`, `list_mt5_windows`, `execute_mt5_computer_action`, `get_server_status`, `greet`, `trading_analysis_template`
>
> **Operational notes:**
> - Tools that take `account_name` internally call `switch_mt5_account` — never call switch manually before another tool.
> - The MT5 terminal at `path` MUST be running. The server attaches to existing terminals; it does not launch them. If you call a tool and get "terminal not initialized," open the MT5 binary first.
> - For `calculate_excursion_analysis`, the underlying tick data must be cached in the terminal. If excursion data returns sparse, open the symbol's chart in MT5 to force tick download.
> - Server logs to stdout. When run via `run_http_server.bat`, watch the cmd window for errors.
>
> **Key files in the repo:**
> - `server.py` — main FastMCP server; all `@mcp.tool` declarations
> - `start_http.py` — single-line HTTP entrypoint
> - `mt5_manager.py` — account-switching + connection lifecycle (singleton manager)
> - `mt5_accounts.json` — account credentials (gitignored)
> - `excursion_calculator.py` — MFE/MAE math
> - `exit_classifier.py` — classifies exits (basket TP / time-stop / SL / trailing)
> - `run_http_server.bat`, `configure_firewall.bat`, `setup_windows.ps1` — Windows operational scripts
> - `README.md`, `QUICK_REFERENCE.md`, `VPS_SETUP.md`, `CLAUDE_CODE_INSTALL.md` — full reference docs in repo root
