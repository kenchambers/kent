# Plan: `kent gateway` — Discord communication channel for the agent

## Context

Today kent only talks to the user through the local REPL or the `kent viz` chat panel. The user wants the agent to live on Discord too — receiving messages, replying in channels and threads, reacting, and showing an online status. The gateway becomes a managed background service controlled via a `kent gateway {run,start,stop,restart,status,config}` lifecycle CLI, optionally launched alongside the viz server from `dev-startup.sh`. The user is prompted once for a Discord bot token (in a separate `kent gateway config` flow, *not* mid-startup).

Design constraints:
- Don't overengineer. One async process, one platform (Discord), reuse the existing tool/loop pattern.
- The agent should be *powerful* on Discord: read history, send, react, manage threads, set presence.
- Each Discord channel/thread maps to a kent **wing** so memory persists per conversation.
- Lifecycle is explicit: foreground `kent gateway run`, plus `start`/`stop`/`restart`/`status`/`config` for daemon control.

## Revisions captured from `/review-plan`

This section enumerates the corrections applied after the v0 review surfaced wiring bugs, scope creep, and missing tests. Each was verified against current source.

| # | v0 plan claim | Reality | Fix |
|---|---|---|---|
| 1 | `_build_system_prompt(..., venue="discord")` | Three positional callers: `cli.py:711`, `cli.py:827`, `agent/training/rollout.py:123`. Adding a kwarg works only with a default — even then, churn is unnecessary. | Don't touch `_build_system_prompt`. Build the Discord prompt inside the gateway entry as `_build_system_prompt(memory_store) + "\n\n" + DISCORD_SUFFIX`. |
| 2 | `cmd_gateway` resolves token via `resolve_api_key` | `resolve_api_key` does `SUPPORTED_SERVICES[service_id]` (cli.py:198) — `KeyError` on `discord_bot_token`. | `cmd_gateway_*` calls `load_credentials().get("discord_bot_token")` directly, then `os.environ.get("KENT_DISCORD_BOT_TOKEN")`, then errors. Never goes through `resolve_api_key`. Do not extend `SUPPORTED_SERVICES`. |
| 3 | `ToolContext` carries `channel_id` | Verified `agent/tools.py:10–14` — `ToolContext(signal, expose_tool_errors)` only. `StreamingExecutor` constructs the context, so subclassing it from a tool is impossible. | Discord tools take `(bot, default_channel_id)` in `__init__` — closure pattern matching `SetWing(memory_store)` and `Spawn(parent_registry, llm, memory_store)`. Each `ChannelSession` builds its own `ToolRegistry` with tools bound to that channel. Tool args still accept an explicit `channel_id` for cross-channel sends. |
| 4 | Subparsers at `cli.py:1341+` | Subparsers actually start at line 1443; `p_viz` is at line 1546. | Insert `p_gateway` after `p_viz` (~line 1551). |
| 5 | `dev-startup.sh:88-97` EXIT trap "just add another PID" | `cleanup()` is hardcoded to `$VIZ_PID` only. | Modify `cleanup()` to also kill `$GATEWAY_PID` with the same `kill -0` guard. |
| 6 | dev-startup.sh placeholder filter passes Discord token through | Line 178 filter strips any value containing `<` — `"<paste-token-here>"` from `credentials.json.example` is silently dropped before sync. | This is fine — token must be entered via `kent gateway config` (or pasted directly into the JSON without `<`). Remove the v0 idea of an interactive prompt inside `dev-startup.sh`. |
| 7 | Wing names `discord/<guild>/<channel>` | `agent/memory/wings.py:7` regex `^[a-z0-9][a-z0-9_-]{0,63}$` rejects `/`. | Use **flat** names with `_` separators: `discord_<guild_id>_<channel_id>` (≤45 chars; guild + channel snowflakes are 18–19 digits each), `discord_dm_<user_id>` (≤30 chars). Both well under the 64-char cap. |
| 8 | Streaming via `msg.edit` debounced every 500ms | Discord rate-limits message edits + 2000-char hard limit. The complexity buys little for a single-user agent, plus the v0 plan never addressed chunking. | **Drop streaming-edit for v1.** Use `async with channel.typing():` for perceived responsiveness, then send the final reply (chunked at 1900-char word boundaries). Streaming becomes a v2 task if/when the user asks. |
| 9 | `ChannelSession` (history + store + lock) is overengineered | Simplifier suggested one shared `MemPalaceStore` with `set_active_wing()` per turn. **This is incorrect:** `_active_wing` is mutable instance state — concurrent turns in different channels would race on the wing field, corrupting `record_turn` / `wake_up_full` semantics. | Keep `ChannelSession`, trimmed: `(history: list[dict], store: MemPalaceStore, lock: asyncio.Lock, registry: ToolRegistry)`. One per active channel. `MemPalaceStore.__init__` is cheap (no I/O), so the per-channel store cost is negligible. |
| 10 | `gateway ready` log polling in `dev-startup.sh` | Viz polling exists because viz binds a port. Discord has no local endpoint; readiness is "@kent in Discord and see a reply." | No polling. Spawn the process, save PID, print `gateway spawned (pid N) — see <log>`. The `kent gateway status` command (described below) gives a richer check on demand. |
| 11 | `discord_read_history` ordering unspecified | `discord.py` returns newest-first by default. | Tool calls `channel.history(limit=limit, oldest_first=True)` and documents "returns chronological". |
| 12 | Discord 2000-char limit not addressed | Hard API constraint. | Add `_split_for_discord(text: str) -> list[str]` helper — splits at word/line boundaries with a 1900-char soft cap (leave headroom for emoji + surrogate pairs). Used for *every* outbound text, including streamed-tool indicator messages. |
| 13 | No tests | — | Full test plan added below in §**Tests**. |
| 14 | No daemon lifecycle commands | User explicitly asked. | Add `kent gateway {run,start,stop,restart,status,config}` and a PID file at `~/.kent/gateway.pid`. See §**CLI lifecycle commands**. |
| 15 | README not updated | User explicitly asked. | See §**README updates**. |

## High-level design

**One async process** (`kent gateway run`) hosts a `discord.py` `commands.Bot` client. The same process owns:
1. The Discord Gateway WebSocket (handled entirely by `discord.py` — heartbeats, resume, intents).
2. A pool of per-channel `ChannelSession` objects, one per active channel/thread.
3. Per-`ChannelSession` `ToolRegistry` containing the standard kent tools *plus* Discord tools (`discord_send`, `discord_react`, `discord_thread_create`, `discord_set_status`, `discord_read_history`). Discord tools close over `(bot, default_channel_id)` per-session.

**Inbound flow** (Discord → agent):
- `on_message` fires when bot is @mentioned (default; `--all-messages` flag widens this).
- Lookup or create `ChannelSession` keyed by `channel.id`.
- Acquire the channel's `asyncio.Lock` so the channel processes one turn at a time. Different channels run concurrently.
- `async with channel.typing():` while the agent loop runs, then send the final reply via `_send_chunked`.

**Outbound flow** (agent → Discord): driven entirely by tools.

## CLI lifecycle commands

```
kent gateway                         # alias for `kent gateway start`
kent gateway run [flags]             # foreground: actually runs the bot loop (this is what dev-startup.sh + start invoke)
kent gateway start [flags]           # detach a child running `kent gateway run`, write ~/.kent/gateway.pid
kent gateway stop                    # SIGTERM the PID, await up to 10s, SIGKILL on timeout, remove pid file
kent gateway restart [flags]         # stop + start (passes flags through to start)
kent gateway status                  # read pid file → alive? print uptime, channel count, log path
kent gateway config                  # interactive: prompt for bot token (getpass), save to credentials.json; show & edit gateway settings in config.json
```

Flags accepted by `run`/`start`/`restart`:
- `--mention-only` / `--all-messages` (default: mention-only)
- `--status online|idle|dnd` (default: online)
- `--activity STR` (default: `"thinking"`)
- `--log-file PATH` (default: `~/.kent/gateway.log`)

`--service`, `--model`, `--wing` accepted on `run`/`start` (forwarded to the underlying `_make_llm`/`MemPalaceStore`).

PID file format: plain integer at `~/.kent/gateway.pid`. Stale-pid handling: if the file exists but the PID is dead (`kill -0` fails), `start` removes it and proceeds; `stop` warns "no running gateway" and removes the stale file.

`config` subactions (no further args = show; with `--token` = update token; with `--mention-only`/`--all-messages` = persist default; with `--reset` = wipe gateway block):
- Stores non-secret defaults under `~/.kent/config.json` key `"gateway": {"mention_only": true, "status": "online", "activity": "thinking"}`.
- Stores `discord_bot_token` in `~/.kent/credentials.json` (chmod 0600 via `save_credentials`).

## Files to create

1. `agent/gateway/__init__.py` — empty package marker.
2. `agent/gateway/discord_bot.py` — main runtime. Defines:
   - `DiscordSettings` dataclass (`mention_only: bool`, `status: str`, `activity: str | None`, `log_file: Path`).
   - `ChannelSession` dataclass (`history`, `store`, `lock`, `registry`, `wing`).
   - `DiscordGateway` class wrapping a `commands.Bot`. Methods: `_session_for(channel) -> ChannelSession`, `on_ready`, `on_message`, `_handle_turn(session, message)`.
   - `_wing_for_channel(message) -> str` — flat-name builder using underscores: `f"discord_{guild_id}_{channel_id}"` or `f"discord_dm_{user_id}"`. Validates length ≤ 64.
   - `_split_for_discord(text: str, limit: int = 1900) -> list[str]` — chunker.
   - `async def run_gateway(token, settings, llm_factory, ...) -> None` — entry point.
3. `agent/gateway/lifecycle.py` — `GatewayLifecycle` helper. Pure functions:
   - `pid_path() -> Path`
   - `read_pid() -> int | None` (returns None if missing/stale)
   - `write_pid(pid: int)`, `clear_pid()`
   - `is_alive(pid: int) -> bool` (uses `os.kill(pid, 0)` with EPERM fallback)
   - `spawn_detached(argv: list[str], log_path: Path) -> int` — uses `subprocess.Popen(start_new_session=True, stdout/stderr → log)`; returns child PID.
   - `stop(pid: int, timeout: float = 10) -> bool` — SIGTERM then SIGKILL; returns True if stopped.
4. `agent/builtin/discord_send.py` — tool. Args: `content: str`, `channel_id: int | None = None`, `reply_to: int | None = None`. Posts a message; uses `_split_for_discord` for chunking (returns `n_messages_sent` in output).
5. `agent/builtin/discord_react.py` — tool. Args: `message_id: int`, `emoji: str`, `channel_id: int | None = None`.
6. `agent/builtin/discord_thread_create.py` — tool. Args: `name: str`, `parent_message_id: int | None = None`, `auto_archive_minutes: int = 1440`.
7. `agent/builtin/discord_set_status.py` — tool. Args: `status: Literal["online","idle","dnd","invisible"]`, `activity: str | None = None`. Calls `bot.change_presence(...)`.
8. `agent/builtin/discord_read_history.py` — tool. Args: `limit: int = 50`, `channel_id: int | None = None`. Calls `channel.history(limit=limit, oldest_first=True)`; returns chronological text.
9. `docs/gateway.md` — Discord application setup walkthrough (token, intents, scopes, invite URL, OAuth permissions). Verbatim block from the original v0 plan, kept here.
10. `tests/test_gateway_cli.py` — argparse, chunking, prompt builder, credential resolution.
11. `tests/test_discord_tools.py` — five tool unit tests (mocked `bot`/`channel`/`message`).
12. `tests/test_gateway_lifecycle.py` — pid file read/write/stale, spawn_detached integration.

## Files to modify

1. **`agent/cli.py`**
   - Add `cmd_gateway_run`, `cmd_gateway_start`, `cmd_gateway_stop`, `cmd_gateway_restart`, `cmd_gateway_status`, `cmd_gateway_config`. Top-level `cmd_gateway` dispatches based on `args.action`.
   - Token resolution helper `_resolve_discord_token() -> str | None`: `load_credentials().get("discord_bot_token")` → `os.environ.get("KENT_DISCORD_BOT_TOKEN")`. **Does not** call `resolve_api_key`.
   - Settings helper `_load_gateway_settings() -> DiscordSettings` reading `load_config().get("gateway", {})` with defaults.
   - Register `gateway` subparser **after `p_viz`** (around line 1551). Subparser uses an `action` argument (positional, choices=`run|start|stop|restart|status|config`, default `start`). All flags above.
   - **Do not** add Discord tools to the local REPL registry (`_repl` at cli.py:703–709).
   - **Do not** change `_build_system_prompt` signature. The Discord suffix is concatenated inside `discord_bot.py`.

2. **`dev-startup.sh`**
   - Modify `cleanup()` (line 88) to also kill `$GATEWAY_PID` (parallel `[[ -n ]]` + `kill -0` guard block).
   - Initialize `GATEWAY_PID=""` near `VIZ_PID=""` (line 30).
   - After the viz launch block (line 251), add a parallel **gateway launch block** that:
     - Skips if `KENT_NO_GATEWAY=1` or no `discord_bot_token` in `~/.kent/credentials.json` (silently — print boot line `gateway disabled (no token — run \`kent gateway config\`)`).
     - Otherwise spawns `uv run --quiet kent gateway run >> "$KENT_HOME/gateway.log" 2>&1 &`, saves `GATEWAY_PID=$!`, prints `gateway spawned (pid $GATEWAY_PID) — log: $KENT_HOME/gateway.log`.
     - **No readiness polling.** Discord has no local endpoint to probe.
   - Print clear instructions in the no-token path: `run \`kent gateway config\` to add a token`.

3. **`credentials.json.example`** — add `"discord_bot_token": "<your-discord-bot-token-here>"`. (Filtered by the dev-startup placeholder rule, which is the desired behavior — token must be set via `kent gateway config` or by pasting a real value.)

4. **`pyproject.toml`** — add `"discord.py>=2.4"` to the `dependencies` array.

5. **`README.md`** — see §**README updates** below.

## Reused existing infrastructure

- `ToolRegistry` / `Tool` protocol: `agent/tools.py:17`. Discord tools follow the closure-over-`(bot, channel_id)` pattern, mirroring how `Spawn` closes over `parent_registry`/`llm`/`memory_store` and `SetWing` over `memory_store`.
- Agent loop: `agent.loop.run` (`agent/loop.py:59`) — drives every Discord turn unchanged. Confirmed yields `TextDelta`, `ToolCallComplete`, `ToolResult`, `AssistantMessageComplete`, `Terminal`.
- Memory: `MemPalaceStore` (`agent/memory/mempalace_store.py:30`) — one instance per `ChannelSession`. Constructor is cheap; `set_active_wing` writes `active_wing.txt` but each session has its own in-memory `_active_wing`. Wing isolation is preserved by **per-session stores** (not shared state).
- Credentials: `load_credentials` / `save_credentials` (`cli.py:179–194`). Both already support arbitrary keys (the dict is untyped), and `save_credentials` chmods 0600. Suitable for `discord_bot_token` without touching `SUPPORTED_SERVICES`.
- Streaming pattern: same `TextDelta`/`ToolCallComplete` event types (`agent/events.py`). Discord's output handler is a third consumer of the same event stream, but for v1 it only needs `Terminal` + final `AssistantMessageComplete` content.
- Startup script credential sync: `dev-startup.sh:165–211` already handles arbitrary keys and chmods 0600 — only the **background launch** + `cleanup()` need adding.

## Tool surface (concrete)

| Tool | Args | Concurrency-safe |
|---|---|---|
| `discord_send` | `content: str`, `channel_id: int \| None = None`, `reply_to: int \| None = None` | yes |
| `discord_react` | `message_id: int`, `emoji: str`, `channel_id: int \| None = None` | yes |
| `discord_thread_create` | `name: str`, `parent_message_id: int \| None = None`, `auto_archive_minutes: int = 1440` | no |
| `discord_set_status` | `status: Literal["online","idle","dnd","invisible"]`, `activity: str \| None = None` | no |
| `discord_read_history` | `limit: int = 50`, `channel_id: int \| None = None` | yes |

`channel_id` defaults to the channel of the inbound message (closed over per-`ChannelSession`). Pass an int to target another channel.

## Wing naming (corrected)

```python
def _wing_for_channel(message) -> str:
    if message.guild is not None:
        return f"discord_{message.guild.id}_{message.channel.id}"
    # DM (DMChannel.guild is None)
    return f"discord_dm_{message.author.id}"
```
Both forms satisfy `^[a-z0-9][a-z0-9_-]{0,63}$` (Discord IDs are decimal snowflakes; total length ≤ 45).

## Discord install instructions (verbatim → `docs/gateway.md`)

> **Step 1 — Create the Discord application**
> 1. Go to https://discord.com/developers/applications
> 2. Click **New Application** (top-right). Name it (e.g. `kent`). Accept ToS.
>
> **Step 2 — Add a bot user and grab the token**
> 1. In the left sidebar of your new app, click **Bot**.
> 2. Click **Reset Token** → **Yes, do it!** → **Copy**. *This is your `discord_bot_token`.* Save it now — Discord won't show it again.
> 3. Scroll down to **Privileged Gateway Intents**. Toggle ON:
>    - **MESSAGE CONTENT INTENT** — required to read message bodies.
>    - **PRESENCE INTENT** — required to observe other users' online status.
>    - **SERVER MEMBERS INTENT** — required for member lists / mentions resolution.
> 4. Click **Save Changes**.
>
> **Step 3 — Generate the invite URL**
> 1. Left sidebar → **OAuth2** → **URL Generator**.
> 2. **Scopes**: check `bot` and `applications.commands`.
> 3. **Bot Permissions**: View Channels, Send Messages, Send Messages in Threads, Create Public Threads, Manage Threads, Read Message History, Add Reactions, Embed Links, Attach Files, Use Slash Commands.
> 4. Copy the **Generated URL** at the bottom and open it in your browser. Pick a server you administer and authorize.
>
> **Step 4 — Save the token**
> ```bash
> kent gateway config              # interactive: pastes token via getpass, persists with chmod 0600
> ```
> Or paste directly into `~/.kent/credentials.json`:
> ```json
> { "atlascloud": "apikey-...", "discord_bot_token": "<paste>" }
> ```
> Then `kent gateway start` (detached) or `kent gateway run` (foreground).

## Tests

Three new test files, all offline-first. Live tests gated by a new `live_discord` marker (analogous to `live_apo`); not run in default `uv run pytest`. Add the marker to `pyproject.toml`'s `[tool.pytest.ini_options].markers`.

### `tests/test_gateway_cli.py` (8 tests, offline)

1. `test_parser_gateway_default_action_is_start` — `kent gateway` parses with `action="start"`.
2. `test_parser_gateway_run_subaction` — `kent gateway run --mention-only --status online`.
3. `test_parser_gateway_status_subaction` — `kent gateway status` parses, dispatches to `cmd_gateway_status`.
4. `test_parser_gateway_config_subaction` — `kent gateway config --token` parses.
5. `test_split_for_discord_chunks_long_text` — text > 1900 chars produces multiple chunks, each ≤ 1900, no word splits.
6. `test_split_for_discord_preserves_short_text` — text ≤ 1900 returns `[text]`.
7. `test_resolve_discord_token_prefers_env` — set `KENT_DISCORD_BOT_TOKEN`, mock empty credentials → returns env value.
8. `test_resolve_discord_token_falls_back_to_credentials` — unset env, monkeypatch `load_credentials` → returns saved value.

### `tests/test_discord_tools.py` (10 tests, offline, mocked bot)

For each of the five tools, two tests using `unittest.mock.AsyncMock`:
- `test_<tool>_happy_path` — bound to a fake bot/channel, calling with valid args invokes the right discord.py method exactly once with expected kwargs.
- `test_<tool>_error_path` — bot method raises `discord.HTTPException` (or import fails) → `ToolResult(is_error=True)` with a stable error string.

Specifics:
- `test_discord_send_chunks_long_content` (additional, 11th test) — verifies a 5000-char `content` results in 3 separate `channel.send` calls.
- `test_discord_read_history_passes_oldest_first_true` — verifies the `oldest_first=True` kwarg lands at the discord.py call site.
- `test_discord_send_uses_default_channel_when_arg_omitted` — verifies the closure-bound `default_channel_id` resolves the channel from `bot.get_channel(default_channel_id)` when args don't specify one.

Use `pytest.importorskip("discord", reason="discord.py not installed")` at module top to skip cleanly when the dep is missing in CI.

### `tests/test_gateway_lifecycle.py` (6 tests, offline)

1. `test_pid_path_uses_kent_home` — respects `KENT_HOME` env var.
2. `test_read_pid_returns_none_when_missing`.
3. `test_read_pid_returns_none_when_stale` — write a PID known not to exist (e.g. `99999999`); `read_pid()` returns `None` and removes the file.
4. `test_write_then_read_roundtrip`.
5. `test_spawn_detached_runs_command` — spawn `python -c "import time; time.sleep(0.5)"`; verify PID is alive immediately, write succeeds, child exits cleanly. Uses `tmp_path` for log file.
6. `test_stop_kills_alive_process` — same spawn pattern, then `stop(pid)` returns True within timeout; pid is gone.

### Integration test (live, gated `live_discord`)

`tests/integration/test_gateway_live.py` — single test, **not run by default**. Requires `KENT_DISCORD_BOT_TOKEN` env var and a `KENT_DISCORD_TEST_CHANNEL_ID` env var. Sends an `@kent ping` programmatically via discord.py's HTTP API, polls for a reply within 30s, asserts a reply appeared. Documented in README under "Testing" alongside `live_apo`.

## What we're explicitly NOT building (v1)

- Slash commands (`app_commands.tree`).
- Voice/audio.
- A second platform (Slack, Telegram, etc.).
- A multi-process daemon with mDNS/Tailscale discovery (OpenClaw's model).
- Persistent message-id ↔ memory-drawer linkage.
- Streaming `msg.edit` reply updates — final-send only for v1.
- Group DMs (3+ users). DMChannel only.

## README updates

Add three new sections (insert points listed):

1. **Table of contents** (line 7–43): add `kent gateway` link under "CLI reference" and "Getting started > 5. Talk to kent on Discord".

2. **Getting started → 5. Talk to kent on Discord** (new section after the existing viz block at line 224):
   - One-paragraph overview: "kent can also live on Discord as a bot — read messages, reply, react, manage threads, set presence — with each channel/DM mapped to its own memory wing."
   - Three commands: `kent gateway config` (set token + see [docs/gateway.md](docs/gateway.md) for the Discord app walkthrough), `kent gateway start`, `kent gateway status`.
   - Note that `dev-startup.sh` auto-starts the gateway when a token is present, parallel to viz.

3. **CLI reference → `kent gateway`** (new subsection after `kent viz` at line 247–258):
   - Table of subactions with one-line descriptions, mirroring the existing `kent viz` table style.
   - Flags table for `run`/`start` (mention-only, status, activity, log-file).
   - Pointer to docs/gateway.md for Discord app setup.
   - Note: requires `discord.py` (installed via `uv sync` once `pyproject.toml` is updated).

4. **Built-in tools** table (line 309–319): append five new rows for `discord_send`, `discord_react`, `discord_thread_create`, `discord_set_status`, `discord_read_history`. Note in a footnote: "Discord tools are only registered inside `kent gateway run`; they require a live Discord WebSocket and won't appear in `kent` REPL or `kent run`."

5. **Configuration** table (line 332–342): add row for `~/.kent/gateway.pid` (PID of running gateway daemon) and row for `~/.kent/gateway.log` (gateway stdout/stderr).

6. **Configuration → environment** table (line 345–349): add `KENT_DISCORD_BOT_TOKEN` (Discord bot token; wins over saved credential), `KENT_NO_GATEWAY=1` (skip gateway in `dev-startup.sh`).

7. **Testing** section (line 442–449): add `uv run pytest -m live_discord` line and a one-sentence note on token + test channel env vars.

## Verification

End-to-end (manual; needs live Discord server):

1. `cp credentials.json.example credentials.json`, paste real `atlascloud` key only.
2. `./dev-startup.sh` → expect: `creds synced N keys`, `viz live at http://...`, `gateway disabled (no token — run \`kent gateway config\`)`.
3. `kent gateway config` → paste token; expect `saved to ~/.kent/credentials.json (chmod 0600)`.
4. `kent gateway start` → expect `gateway started (pid N) — log: ~/.kent/gateway.log`. `kent gateway status` → expect `running (pid N, uptime Xs, channels 0)`.
5. In Discord, `@kent hello` → bot replies. Verify wing `discord_<guild_id>_<channel_id>` exists in `~/.kent/diaries/`.
6. `@kent please react to my last message with 🔥` → bot calls `discord_react`, reaction appears.
7. `@kent split this into a thread called "side topic"` → bot calls `discord_thread_create`, posts in the new thread.
8. `@kent set yourself to do-not-disturb` → status changes.
9. DM the bot → expect a session in wing `discord_dm_<your_user_id>`.
10. `@kent` ask for a 3000-char output → bot replies in 2 messages, no truncation.
11. `kent gateway stop` → expect `stopped (pid N)`. `kent gateway status` → `not running`.
12. Re-run `./dev-startup.sh` with token now present → gateway spawns alongside viz; Ctrl-C the REPL → `cleanup()` kills both viz and gateway PIDs (verify with `ps`).

Sanity checks:
- `uv run pytest tests/test_gateway_cli.py tests/test_discord_tools.py tests/test_gateway_lifecycle.py` → all green.
- `uv run pytest -m "not integration and not memory and not slow and not live_discord"` → 196 + 24 = 220 tests green.
- `uv run kent gateway --help` shows all subactions and flags.
- `python -c "from agent.gateway.discord_bot import DiscordGateway, _split_for_discord, _wing_for_channel"` imports cleanly.
- Existing `kent run` and `kent viz` paths unchanged (Discord tools not in their registries; `_build_system_prompt` unchanged).
- `python -c "from agent.training.rollout import _build_system_prompt"` still resolves (we didn't change the symbol).
