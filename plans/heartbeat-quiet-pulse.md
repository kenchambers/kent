# Heartbeat for the kent gateway

## Context

The Discord gateway daemon (`agent/gateway/discord_bot.py`) currently only does work when a Discord message arrives — it sits idle on its event loop between mentions. We want a periodic, agent-driven "check-in" that fires on a cron-like cadence, modeled after OpenClaw's heartbeat (configurable interval, instructions in a file the agent and user can both edit). On each tick the agent reads `HEARTBEAT.md`, runs one full agent turn with that file as the prompt, and uses its existing tools (`discord_send`, `diary_write`, etc.) to act. This keeps the agent "alive" between user interactions without bolting on a separate scheduler process.

Decisions confirmed with user:
- **Tick behavior:** run one full agent turn each tick, prompt = contents of `HEARTBEAT.md`.
- **Run location:** background asyncio task inside the gateway daemon (no separate process).
- **Default interval if user just hits enter at the startup prompt:** `30m`.

## Recommended approach

### 1. New module: `agent/gateway/heartbeat.py`

Self-contained, ~100 lines. Owns:

- `parse_interval(s: str) -> float | None` — accepts `"30s"`, `"5m"`, `"30m"`, `"1h"`, and the off-tokens `""`, `"off"`, `"disabled"`, `"0"`, `"none"`. Returns seconds, or `None` for off. Reuses the parsing rules from `agent/cli.py:1219` `_parse_duration` (don't import the private symbol — keep heartbeat.py free of cli imports; one tiny duplicate is cheaper than the coupling).
- `read_heartbeat_md(path: Path) -> str` — reads `~/.kent/HEARTBEAT.md`. If missing, returns empty string and the loop skips the tick (logs once). The path is resolved fresh each tick so the user can edit the file live.
- `default_heartbeat_md_text() -> str` — seed contents written by `dev-startup.sh` if the file doesn't exist. Short skeleton: a one-line description + a few bullet examples ("post a status", "review recent diary entries", "reach out in #channel-name if X"). Plain prose; the agent reads it as a user message.
- `class Heartbeat`:
  - `__init__(self, *, gateway, interval_s, md_path, channel_id)` — stores deps.
  - `start() -> asyncio.Task` — schedules the loop; returns the task so the caller can cancel.
  - `async def _loop(self)` — `while True: await asyncio.sleep(interval); await self._tick()`. Catches and logs every exception so a single bad tick can't kill the loop.
  - `async def _tick(self)` — calls `gateway._run_heartbeat_turn(...)` (added in step 2).
  - Writes `last_heartbeat_at` (and `last_heartbeat_status`) into the gateway status snapshot via `_lc.write_status` (gateway re-merges its own keys on the next snapshot).

### 2. Wire into `DiscordGateway` (`agent/gateway/discord_bot.py`)

Add three pieces:

a. **Settings field.** Extend `DiscordSettings` (line 38) with:
```
heartbeat_interval: str | None = None    # "30m", "off", or None (= off)
heartbeat_channel_id: int | None = None  # which channel session the tick runs against
```

b. **Lifecycle.** In `DiscordGateway.__init__` add `self._heartbeat: Heartbeat | None = None`. In `on_ready` (line 176), after `_write_status_snapshot()`, instantiate and start the heartbeat *only if* `interval` parses to a positive number AND `heartbeat_channel_id` is set. In `close()` (line 310), cancel the task and `await` it before closing the bot.

c. **Tick handler.** New `async def _run_heartbeat_turn(self, channel_id: int, prompt_text: str)`:
- Build a synthetic shim that has `.channel.id == channel_id` so `_session_for` works unchanged, OR add a small `_session_for_channel_id(cid: int)` helper that contains the same lazy-create logic factored out. Prefer the helper — it's three lines and avoids the shim hack. Rewire `_session_for(message)` to call `_session_for_channel_id(int(message.channel.id))` so there's one code path.
- Acquire `session.lock` (same as `on_message`) — guarantees we never run an LLM call concurrently with a user-driven turn in the same channel.
- Run `agent_run(...)` with `messages=[*session.history, {"role": "user", "content": prompt_text}]`, identical loop body to `_handle_turn` lines 270–296 but **without** the `message.channel.typing()` context (no incoming message) and **without** the auto-send block at lines 298–305. The agent decides whether to post via `discord_send`. Recording history follows the same pattern.
- On exception, log via `logger.exception("heartbeat turn failed")`. Do not raise — heartbeat must be self-healing.

### 3. Config + CLI plumbing (`agent/cli.py`)

a. Extend `_load_gateway_settings` (line 1405) to read `block.get("heartbeat_interval")` and `block.get("heartbeat_channel_id")` from `cfg["gateway"]`, with env overrides `KENT_HEARTBEAT_INTERVAL` and `KENT_HEARTBEAT_CHANNEL_ID`. Pass both into `DiscordSettings`.

b. Extend `cmd_gateway_config` (line 1626) to print the two new fields under `[gateway settings]` and accept `--heartbeat-interval STR` / `--heartbeat-channel-id INT` flags (mirror the existing `--status` / `--activity` pattern, both in `_build_parser` line 1862 and in this function's `if getattr(args, ..., None):` block).

c. **Optional polish, not required:** add `kent gateway heartbeat-now` action that triggers one tick immediately by signalling the running daemon (skip if it complicates things — restart is fine).

### 4. Startup prompt (`dev-startup.sh`)

Insert a new section between the existing 4b gateway-spawn block (lines 261–287) and the REPL drop. Pseudocode:

```
if [token present] and [config.json has no gateway.heartbeat_interval]:
    prompt user: "How often should the heartbeat tick? (30s/5m/30m/1h/off, default 30m): "
    read INTERVAL (default = "30m" on empty)
    if INTERVAL != "off":
        also prompt: "Heartbeat Discord channel id (numeric, blank = skip): "
    write both into config.json["gateway"] via a small `uv run python -` heredoc
        (same pattern used at lines 173–219 for credentials sync)
if HEARTBEAT.md missing in $KENT_HOME:
    seed it with default_heartbeat_md_text() (call via `uv run python -c ...`)
boot_line "hb   " "tick=<interval> channel=<id> file=$KENT_HOME/HEARTBEAT.md"
```

Skip the prompt if `KENT_NO_HEARTBEAT=1`. Skip the prompt if the value is already set in `config.json` (so subsequent runs don't nag).

### 5. `HEARTBEAT.md`

Lives at `${KENT_HOME}/HEARTBEAT.md` (resolves via `agent/gateway/lifecycle.py:13` `_kent_home()`). Don't ship a file in the repo — `dev-startup.sh` seeds it on first run from `default_heartbeat_md_text()` so the file is per-install, not source-controlled. Free-form Markdown; the loop just reads it as a string each tick.

### 6. Tests

Add `tests/test_heartbeat.py`:
- `parse_interval` table-driven cases (valid / invalid / off-tokens).
- `read_heartbeat_md` returns `""` when file missing, returns content otherwise.
- `Heartbeat._loop` runs a tick at the configured interval — fake `asyncio.sleep`, fake `gateway._run_heartbeat_turn` (assert called with the right prompt), assert exception in one tick doesn't break the next.

Extend `tests/test_gateway_lifecycle.py` if any new lifecycle helpers are added (unlikely — we reuse `write_status`).

Skip live Discord tests; the existing opt-in `tests/integration/test_gateway_live.py` already covers WebSocket reachability.

## Critical files

| Path | Action |
|---|---|
| `agent/gateway/heartbeat.py` | NEW — module described in §1 |
| `agent/gateway/discord_bot.py` | MODIFY — `DiscordSettings` (line 38), `__init__` (line 138), `on_ready` (line 176), `close` (line 310), add `_session_for_channel_id` and `_run_heartbeat_turn` |
| `agent/gateway/__init__.py` | MODIFY — re-export `Heartbeat`, `parse_interval` |
| `agent/cli.py` | MODIFY — `_load_gateway_settings` (line 1405), `cmd_gateway_config` (line 1626), `_build_parser` gateway block (line 1862), `_build_run_argv` (line 1502) so detached `start` propagates the new flags |
| `dev-startup.sh` | MODIFY — insert §4 prompt block before line 289 |
| `tests/test_heartbeat.py` | NEW |

## Functions / utilities to reuse

- `agent/gateway/lifecycle.py:13` `_kent_home()` — resolve the `~/.kent` directory consistently.
- `agent/gateway/lifecycle.py:25` `write_status()` — append `last_heartbeat_at` to status JSON.
- `agent/cli.py:1219` `_parse_duration` — duration parsing rules (we re-implement, don't import).
- `agent/loop.py` `run` (imported as `agent_run`) — agent turn driver; reuse the exact loop body from `_handle_turn` lines 272–285.
- `agent/gateway/discord_bot.py:215` `_session_for` — refactor into `_session_for_channel_id(cid)` so heartbeat and `on_message` share one session-creation path.
- `agent/builtin/discord_send.py` — the agent already has this tool registered via `_build_discord_registry`; no changes needed, the heartbeat agent picks it up automatically.

## Verification

1. **Unit:** `uv run pytest tests/test_heartbeat.py tests/test_gateway_cli.py tests/test_gateway_lifecycle.py -q` — all green.
2. **Static:** `uv run mypy agent/gateway/heartbeat.py agent/gateway/discord_bot.py` (or pyright per `pyrightconfig.json`).
3. **Startup script:** run `./dev-startup.sh` with no `gateway.heartbeat_interval` in config — confirm the prompt appears, default `30m` accepted on empty input, value written to `~/.kent/config.json`, `HEARTBEAT.md` seeded, gateway spawns.
4. **Live tick:** with a real Discord token + test channel id, set interval to `30s`, edit `HEARTBEAT.md` to "Post the message 'hb-test-<random>' in this channel.", `kent gateway run` in foreground, watch the channel for one post within ~30s, then edit HEARTBEAT.md to a different instruction and confirm the next tick uses the new content (proves live-reload).
5. **Status:** `kent gateway status` shows `last_heartbeat_at` with a recent timestamp.
6. **Resilience:** temporarily corrupt `HEARTBEAT.md` (delete it), confirm the loop logs and continues without crashing the gateway; restore the file and confirm ticks resume.
7. **Off path:** set interval to `off`, restart gateway, confirm no heartbeat task is created (grep `gateway.log` — no "heartbeat" lines).
