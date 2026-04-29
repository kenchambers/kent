# kent

A minimal, typed async agent runtime for OpenAI-compatible LLMs, plus a `kent` CLI for using it interactively from the terminal — in the spirit of [opencode](https://opencode.ai/) and [hermes-agent](https://hermes-agent.nousresearch.com/), but small enough to read in one sitting.

The Python package is imported as `agent`; the installed CLI binary is `kent`.

## Table of contents

- [What this is](#what-this-is)
- [Repo layout](#repo-layout)
- [Install](#install)
- [Quick start (dev)](#quick-start-dev)
- [Getting started](#getting-started)
  - [1. Set your API key](#1-set-your-api-key)
  - [2. Launch the REPL](#2-launch-the-repl)
  - [3. One-shot mode](#3-one-shot-mode)
  - [4. Open the live 3D palace viewer + chat](#4-open-the-live-3d-palace-viewer--chat)
  - [5. Talk to kent on Discord](#5-talk-to-kent-on-discord)
- [CLI reference](#cli-reference)
  - [`kent`](#kent-1)
  - [`kent run`](#kent-run)
  - [`kent viz`](#kent-viz)
  - [`kent gateway`](#kent-gateway)
  - [`kent auth`](#kent-auth)
  - [`kent models`](#kent-models)
  - [`kent doctor`](#kent-doctor)
  - [Slash commands (in-REPL)](#slash-commands-in-repl)
- [Built-in tools](#built-in-tools)
- [Supported services](#supported-services)
- [Configuration](#configuration)
- [Library use](#library-use)
  - [Minimal example](#minimal-example)
  - [Tool authoring](#tool-authoring)
  - [Subagent example](#subagent-example)
  - [Event reference](#event-reference)
  - [Cancellation](#cancellation)
- [Testing](#testing)
- [Training & evaluation](#training--evaluation)
  - [Commands](#commands)
  - [Test layout](#test-layout-31-total-in-teststraining)
  - [What's been verified live](#whats-been-verified-live)
  - [Suggested next tests](#suggested-next-tests)
- [Persistent memory](#persistent-memory)
  - [Wings & diary](#wings--diary)
- [Known limitations](#known-limitations)

## What this is

Two layers in one repo:

1. **A library** (`agent`) — a ~400-line agent loop that streams events, starts safe tool calls *while the model is still streaming*, partitions concurrent vs. serial tools, and recovers from context-window overflow by compacting and retrying. Works against anything OpenAI-shaped: OpenAI, Atlas Cloud, Together, Groq, OpenRouter, vLLM, Ollama, llama.cpp.
2. **A CLI** (`kent`) — a small terminal front-end that auto-detects your shell, prompts for a service / model / key on first run, persists the choice, and drops you into a REPL with web-search, web-fetch, shell, and subagent tools wired up. Ships subcommands (`run`, `auth`, `models`, `doctor`) so it's scriptable too.

Web search uses **DuckDuckGo HTML scraping** — no third-party search API key required.

## Repo layout

```
agent/
├── __init__.py        # public exports
├── cli.py             # `kent` CLI: subcommands, REPL, slash commands, persistence
├── loop.py            # the agent loop (streams events, drives tools, handles overflow)
├── llm.py             # LLM protocol + OpenAICompatibleLLM (driven by openai SDK)
├── tools.py           # Tool protocol, ToolRegistry, StreamingExecutor (concurrent/serial batching)
├── state.py           # immutable LoopState, terminal/transition reasons
├── events.py          # all event dataclasses (TextDelta, ToolCallComplete, Terminal, …)
├── compact.py         # context-window compaction + recovery
├── builtin/
│   ├── shell.py       # cross-platform shell tool (bash / wsl / powershell)
│   ├── spawn.py       # spawn_subagent: delegate a subtask with its own context window
│   ├── web_search.py  # DuckDuckGo HTML scraping (no API key)
│   ├── web_fetch.py   # URL → markdown via httpx + markdownify
│   ├── memory_recall.py / memory_recall_here.py / diary_write.py / set_wing.py
│   └── task_boundary.py  # task_start, task_end (rollout boundaries for training)
└── training/          # APO training subsystem (Microsoft Agent Lightning + MemPalace)
    ├── rollout.py            # @agl.rollout-decorated kent_task_rollout + recall_game_rollout
    ├── apo_runner.py         # train_resource() — wraps agl.Trainer.fit() with APO
    ├── palace_isolation.py   # snapshot/cleanup helpers (hardlink + SQLite/diary copy)
    ├── critic_scorer.py      # critic LLM call + JSON parse + scalar reward
    ├── swap_pair.py          # actor×critic family-collision guard
    ├── recall_games.py       # Game A: query → Layer3.search → recall@k
    ├── scope_eval.py         # Game B: counterfactual scope selection
    ├── closet_fidelity.py    # Game C: can actor answer from closet alone?
    ├── tunnel_utility.py     # Game D logger
    ├── eval_harness.py       # collusion probes + cross-critic consensus
    └── datasets.py           # TrainingExample loaders

tests/                 # pytest suite (offline + opt-in integration tests)
└── training/          # 31 tests for the training subsystem (see Training & evaluation §)
```

## Install

From PyPI (when published):

```bash
uv add agent
```

From a clone:

```bash
git clone <repo-url> kent
cd kent
uv sync
```

This installs the `kent` binary into the project venv. Either run it via `uv run kent …` or activate the venv (`source .venv/bin/activate`) and use `kent` directly.

## Quick start (dev)

The repo ships a one-shot bootstrap script that installs dependencies, validates your API keys, and drops you into a chat session:

```bash
./dev-startup.sh
```

What it does:

1. Runs `uv sync` to install all project + dev dependencies into `.venv`.
2. Reads `credentials.json` at the repo root and filters out placeholder values (anything containing `<`, e.g. `apikey-<your-atlascloud-key-here>`).
3. Merges valid keys into `~/.kent/credentials.json` (chmod 0600) — the location `kent` resolves keys from.
4. Launches `kent run "user just finished installation of kent repo"` so the LLM greets you with post-install context.

Setup before running:

```bash
cp credentials.json.example credentials.json
# edit credentials.json and replace the placeholder with your real key
./dev-startup.sh
```

If `credentials.json` is missing or only contains placeholder values, the script stops cleanly after `uv sync` and prints `kent auth` instructions instead of launching chat. `credentials.json` is gitignored.

## Getting started

### 1. Set your API key

For Atlas Cloud (the only service wired up out of the box):

```bash
export ATLASCLOUD_API_KEY=apikey-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

…or save it persistently with chmod-600 storage:

```bash
kent auth
# prompts for the key, writes to ~/.kent/credentials.json
```

Resolution order: env var → saved credential → interactive prompt.

### 2. Launch the REPL

```bash
kent
```

You'll see:

```
============================================================
 kent — interactive terminal AI agent
============================================================

[environment]
  OS         : Darwin 24.3.0
  Python     : 3.13.0
  Shell tool : bash (macOS)  (/bin/bash)

[web search]
  Provider   : DuckDuckGo HTML  (https://html.duckduckgo.com/html/)
  API key    : none required
  Notes      : DDG may rate-limit; this is best-effort scraping.
               No queries are sent to any third-party search API.

[llm setup]
Service (atlascloud) [atlascloud]:
Model (qwen/qwen3.6-35b-a3b) [qwen/qwen3.6-35b-a3b]:

[ready]
  Service: Atlas Cloud  (https://api.atlascloud.ai/v1)
  Model  : qwen/qwen3.6-35b-a3b
  Tools  : web_search, web_fetch, shell, spawn_subagent
  Type your message. /help for slash commands. /exit to quit.
------------------------------------------------------------

you>
```

After the first run, `~/.kent/config.json` remembers your service and model — subsequent launches just press-Enter through the prompts.

### 3. One-shot mode

Skip the REPL when you only want a single answer:

```bash
kent run "What's in my current directory?"
kent run "Find the latest Python release version" --quiet     # suppress tool-call chatter
kent run "Summarize https://peps.python.org/pep-0008/"
```

Exits 0 on success, 1 on `model_error` / `context_overflow` / `tool_loop`, 2 on missing config.

### 4. Open the live 3D palace viewer + chat

`kent viz` starts a tiny localhost web server that renders your MemPalace as an animated 3D force-directed graph **and** gives you a chat panel so you can talk to kent in the same window. As the agent's tool calls (`diary_write`, `set_wing`, `memory_recall`, …) hit disk, you watch new drawers bloom into the graph in real time.

```bash
kent viz                       # start on the default port (8765)
kent viz --port 9000           # pick a different port
kent viz --read-only           # graph only, no chat panel (no API key needed)
```

You'll see:

```
kent viz [chat+graph] → http://127.0.0.1:8765
```

Open that URL in any modern browser. Ctrl-C in the terminal to stop the server.

The page is one static HTML file with two SSE streams under the hood: `/events` pushes a fresh palace snapshot whenever the on-disk mtime ticks (~1 s), and `POST /chat` streams agent events back into the right-hand chat column. No build step, no npm — `3d-force-graph` and `three.js` load from `cdn.jsdelivr.net` on first paint.

Requirements: `mempalace` must be installed (it is, by default — `uv sync` pulls it in). For the chat panel, `kent auth` (or `ATLASCLOUD_API_KEY`) must be set; pass `--read-only` to skip the LLM/auth setup. The server binds `127.0.0.1` only — there is no auth, by design.

### 5. Talk to kent on Discord

kent can also live on Discord as a bot — read messages, reply, react, manage threads, set presence — with each channel/DM mapped to its own memory wing. The gateway is a managed background service; once a token is saved, `dev-startup.sh` auto-starts it alongside `kent viz`.

```bash
kent gateway config             # paste your bot token (see docs/gateway.md for app setup)
kent gateway start              # detach the daemon
kent gateway status             # is it running? where's the log?
```

See [docs/gateway.md](docs/gateway.md) for the full Discord application walkthrough (creating the app, enabling intents, generating an invite URL).

## CLI reference

### `kent`

Launch the interactive REPL. No arguments. Prints the banner, environment, and web-search notice; prompts for service / model / key (using saved values as defaults); enters a streaming REPL loop.

### `kent run`

```
kent run <prompt> [--service ID] [--model ID] [--quiet]
```

| Option       | Default              | What it does                                            |
|--------------|----------------------|---------------------------------------------------------|
| `<prompt>`   | (required)           | The user message                                        |
| `--service`  | saved or `atlascloud`| Override the service for this call                      |
| `--model`    | saved or service default | Override the model for this call                    |
| `--quiet`    | off                  | Suppress the `→ tool(...)` / `← [OK]` chatter           |

### `kent viz`

```
kent viz [--port N] [--read-only]
```

Launches the live 3D palace viewer + chat panel on `http://127.0.0.1:<port>`. The graph auto-updates from the on-disk palace via mtime polling; the chat panel runs the same agent stack as `kent` / `kent run` and writes back into the same palace.

| Option         | Default | What it does                                                    |
|----------------|---------|-----------------------------------------------------------------|
| `--port`       | `8765`  | Port to bind on `127.0.0.1`                                     |
| `--read-only`  | off     | Disable the chat panel; render the palace only (no API key needed) |

Exits 0 on Ctrl-C, 1 if `mempalace` isn't importable, 2 if chat is enabled but no API key is configured (run `kent auth` or pass `--read-only`).

### `kent gateway`

```
kent gateway [run|start|stop|restart|status|config] [flags]
```

Runs kent as a Discord bot. Each channel/DM maps to its own memory wing (`discord_<guild_id>_<channel_id>` or `discord_dm_<user_id>`). Discord tools (`discord_send`, `discord_react`, `discord_thread_create`, `discord_set_status`, `discord_read_history`) are registered into the bot's tool registry — they are *not* available in the local REPL or `kent run`.

| Subaction  | What it does                                              |
|------------|-----------------------------------------------------------|
| `run`      | Foreground — runs the bot loop until Ctrl-C / disconnect  |
| `start`    | Detach a daemon child; write `~/.kent/gateway.pid`        |
| `stop`     | SIGTERM the pid (await 10s), SIGKILL on timeout, clear pid |
| `restart`  | `stop` then `start`                                       |
| `status`   | Print pid, uptime, channel count, ready timestamp, log path |
| `config`   | Prompt for bot token (chmod 0600), edit gateway defaults  |

Flags for `run` / `start` / `restart`:

| Flag             | Default              | What it does                                                |
|------------------|----------------------|-------------------------------------------------------------|
| `--mention-only` | on                   | Only respond when @-mentioned                               |
| `--all-messages` | off                  | Respond to every message in visible channels                |
| `--status`       | `online`             | Initial presence: `online`/`idle`/`dnd`/`invisible`         |
| `--activity`     | `thinking`           | "Playing X" / "Watching X" string                           |
| `--log-file`     | `~/.kent/gateway.log`| Where the detached process writes stdout/stderr             |
| `--service`      | (saved config)       | Override LLM service (e.g. `atlascloud`) for this run       |
| `--model`        | (saved config)       | Override model id for this run                              |
| `--wing`         | (per-channel auto)   | Pin every channel/DM to a single wing (overrides per-channel naming) |

Status side-files: `~/.kent/gateway.pid` is written at start; `~/.kent/gateway.status.json` is updated on connect and per new channel session — both are removed on `kent gateway stop`.

Requires `discord.py` (installed via `uv sync` once `pyproject.toml` is updated). See [docs/gateway.md](docs/gateway.md) for the Discord application walkthrough.

### `kent auth`

```
kent auth [--service ID] [--clear]
```

Save (or clear) an API key for a service. Stored at `~/.kent/credentials.json` with `chmod 0600` attempted.

| Option       | Default      | What it does                              |
|--------------|--------------|-------------------------------------------|
| `--service`  | `atlascloud` | Which service the key is for              |
| `--clear`    | off          | Remove the saved credential and exit      |

### `kent models`

```
kent models [--service ID]
```

Lists the models available for a service. Marks the default with `(default)` and the currently-active saved choice with `*`.

### `kent doctor`

```
kent doctor
```

Health check. Prints OS / shell backend, web-search provider, config-file paths, per-service credential status (env var present? saved credential present?), and a dependency-import check. Useful first thing to run if anything misbehaves.

### Slash commands (in-REPL)

| Command                  | What it does                                       |
|--------------------------|----------------------------------------------------|
| `/help`                  | Show the slash command list                        |
| `/tools`                 | List registered tools                              |
| `/model`                 | Show service / model / context window              |
| `/clear`                 | Clear conversation history (keep the session)      |
| `/memory`                | Show palace path, transcript path, session ID, active wing |
| `/recall <query>`        | Global semantic search over all drawers            |
| `/recall-here <query>`   | Wing-scoped diary search (active wing only)        |
| `/forget`                | Delete the current session's transcript (with confirmation) |
| `/wing`                  | Show current active wing and its intent            |
| `/wing <name>`           | Switch to a wing (must already exist)              |
| `/wings`                 | List all wings with intents; `*` marks the active one |
| `/diary <text>`          | Append an OBSERVATION to the active wing's diary   |
| `/exit`, `/quit`         | Leave the session                                  |

## Built-in tools

| Tool                  | What it does                                                         | API key | Concurrency-safe |
|-----------------------|----------------------------------------------------------------------|---------|------------------|
| `web_search`          | DuckDuckGo HTML scraping — returns `[{title, url, snippet}]`         | none    | yes              |
| `web_fetch`           | URL → markdown via httpx + markdownify (10 MB cap, 100K char output) | none    | yes              |
| `shell`               | Host shell (bash on macOS/Linux/WSL, PowerShell on Windows)          | none    | no               |
| `spawn_subagent`      | Delegate a focused subtask with its own context window               | none    | yes              |
| `memory_recall`       | Global semantic search over all session drawers                      | none    | yes              |
| `memory_recall_here`  | Wing-scoped semantic search over the active wing's diary             | none    | yes              |
| `diary_write`         | Append an entry (OBSERVATION / FINDING / DECISION / PATTERN) to the active wing's diary | none | no |
| `set_wing`            | Switch to or register a named project wing                           | none    | no               |
| `discord_send`†       | Post a message to a Discord channel (chunked at 1900 chars)          | bot token | yes            |
| `discord_react`†      | Add a reaction emoji to a Discord message                            | bot token | yes            |
| `discord_thread_create`† | Open a public thread (with optional parent message anchor)        | bot token | no             |
| `discord_set_status`† | Change the bot's presence (online / idle / dnd / invisible)          | bot token | no             |
| `discord_read_history`† | Read recent messages in chronological order                        | bot token | yes            |

† Discord tools are only registered inside `kent gateway run`; they require a live Discord WebSocket and won't appear in `kent` REPL or `kent run`.

Concurrency-safe tools batch and run in parallel via `StreamingExecutor`; unsafe tools (like `shell`) serialize so they can't race state mutations.

## Supported services

| Service     | Default model              | Base URL                          | Env var                |
|-------------|----------------------------|-----------------------------------|------------------------|
| atlascloud  | `qwen/qwen3.6-35b-a3b`     | `https://api.atlascloud.ai/v1`    | `ATLASCLOUD_API_KEY`   |

Adding a new service: edit `SUPPORTED_SERVICES` in `agent/cli.py` — it's a dict literal. For library use, just instantiate `OpenAICompatibleLLM(base_url=..., api_key=..., model=..., context_window=...)` directly.

## Configuration

Files live under `~/.kent/` (override with `KENT_HOME=/some/path`):

| File / Dir                           | Contents                              | Notes                          |
|--------------------------------------|---------------------------------------|--------------------------------|
| `~/.kent/config.json`                | `{service_id, model}`                 | Non-secret; written on first run |
| `~/.kent/credentials.json`           | `{<service_id>: <api_key>, …}`        | Written by `kent auth`; chmod 0600 |
| `~/.kent/active_wing.txt`            | Current wing name (one line)          | Updated by `set_wing` tool / `/wing` / `--wing` |
| `~/.kent/diaries/<wing>/`            | Per-wing diary directory              | Created on first diary write |
| `~/.kent/diaries/<wing>/.intent.txt` | One-line wing description             | Written at wing creation |
| `~/.kent/diaries/<wing>/YYYY-MM-DD.md` | Daily diary entries               | Append-only; ingested into palace |
| `~/.kent/gateway.pid`                | PID of the running gateway daemon     | Written by `kent gateway start`; cleared by `stop` |
| `~/.kent/gateway.status.json`        | Live snapshot: connected user, channel count, ready timestamp | Updated by the daemon on connect + each new channel session; cleared by `stop` |
| `~/.kent/gateway.log`                | Gateway stdout/stderr                 | Append-only; rotates on `dev-startup.sh` boot |

Override with environment:

| Variable               | Effect                                                       |
|------------------------|--------------------------------------------------------------|
| `KENT_HOME`            | Use a different config dir (default `~/.kent`)               |
| `KENT_WING`            | Set the active wing for a session (overrides `active_wing.txt`) |
| `ATLASCLOUD_API_KEY`   | Atlas Cloud API key — wins over saved credential             |
| `KENT_DISCORD_BOT_TOKEN` | Discord bot token — wins over saved credential             |
| `KENT_NO_GATEWAY=1`    | Skip launching the Discord gateway in `dev-startup.sh`       |

## Library use

### Minimal example

```python
import asyncio
from pydantic import BaseModel
from agent import run, ToolRegistry, ToolResult, OpenAICompatibleLLM, TextDelta, Terminal

class EchoTool:
    name = "echo"
    description = "Echo back the input text"
    class Args(BaseModel):
        text: str
    input_model = Args
    def is_concurrency_safe(self, args): return True
    async def call(self, args, ctx):
        return ToolResult(call_id="", output=args.text)

async def main():
    llm = OpenAICompatibleLLM("http://localhost:11434/v1", "ollama", "llama3.2", context_window=8192)
    registry = ToolRegistry()
    registry.register(EchoTool())
    async for ev in run(messages=[{"role": "user", "content": "say hello"}], tools=registry, llm=llm):
        if isinstance(ev, TextDelta):
            print(ev.text, end="", flush=True)
        if isinstance(ev, Terminal):
            print(f"\n[{ev.reason}]")

asyncio.run(main())
```

### Tool authoring

```python
from pydantic import BaseModel
from agent import ToolResult, ToolContext

class MyTool:
    name = "my_tool"               # unique tool name
    description = "What it does"   # shown to the model
    class Args(BaseModel):
        path: str                  # Pydantic model for arguments
    input_model = Args
    def is_concurrency_safe(self, args) -> bool:
        return True   # True = may run in parallel with other safe tools
    async def call(self, args: Args, ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id="", output=f"result for {args.path}")
```

### Subagent example

```python
from agent import ToolRegistry, OpenAICompatibleLLM
from agent.builtin.spawn import Spawn

registry = ToolRegistry()
llm = OpenAICompatibleLLM(...)
registry.register(Spawn(parent_registry=registry, llm=llm))
# model can now call spawn_subagent to delegate subtasks
```

### Event reference

| Event | When |
|---|---|
| `TurnStart(turn)` | New turn begins |
| `TextDelta(text)` | Streaming text token |
| `ThinkingDelta(text)` | Streaming thinking token (extended thinking) |
| `ToolCallStart(call_id, name)` | Tool call starts streaming |
| `ToolCallDelta(call_id, args_json_delta)` | Incremental tool args |
| `ToolCallComplete(call)` | Tool call fully parsed |
| `AssistantMessageComplete(message)` | Full assistant turn |
| `ToolResult(call_id, output, is_error)` | Tool execution result |
| `ContextOverflow(error)` | Context window exceeded (after recovery attempt) |
| `ModelError(error)` | Unrecoverable LLM error |
| `MaxTurnsReached(turn)` | Hit `max_turns` limit |
| `ToolLoopDetected(calls)` | Same tool calls repeated 3+ times |
| `Terminal(reason)` | Loop ended; reason in `TerminalReason` |

### Cancellation

Pass `signal: asyncio.Event` to `run()`. Set it from another task to abort:

```python
signal = asyncio.Event()
asyncio.create_task(cancel_after_timeout(signal))
async for ev in run(..., signal=signal):
    ...
```

## Testing

```bash
uv run pytest -m "not integration and not memory and not slow"   # offline suite (default)
uv run pytest tests/training/                                    # training subsystem only
uv run pytest -m live_apo -v -s                                  # live LLM + APO tests (Atlas Cloud key required)
uv run pytest -m live_discord -v -s                              # live Discord gateway (requires KENT_DISCORD_BOT_TOKEN + KENT_DISCORD_TEST_CHANNEL_ID)
uv run pytest tests/integration/                                 # live mempalace / ollama
```

The offline suite covers the agent loop, streaming executor, compaction, the `Spawn` subagent, every built-in tool, and the full training subsystem (palace isolation, critic scoring, swap-pair guard, recall games, rollout pipeline). **196 tests, all green** in ~2.3s.

| Suite | Marker | Count | Wall time |
|---|---|---|---|
| Core unit | `not integration and not memory and not slow` | 196 | 2.3s |
| Training subset | `tests/training/ and not integration and not live_apo` | 25 | 1.7s |
| Live LLM | `live_apo` | 3 | varies (10s – 10min+) |
| Live mempalace / ollama | `integration` | 53 (5 currently failing — opt-in) | minutes |

## Training & evaluation

The training subsystem optimizes kent's prompt resources via Microsoft Agent Lightning's APO (Automatic Prompt Optimization) — see [the plan](plans/lets-assume-that-we-happy-sunrise.md). Two CLI entry points and a tiered test ladder validate it.

### Commands

```bash
kent train --resource query_rewrite_policy \
    --pair qwen/qwen3.6-35b-a3b+qwen/qwen3.6-35b-a3b \
    --apo-base-url https://api.atlascloud.ai/v1 \
    --gradient-model qwen/qwen3.6-35b-a3b \
    --apply-edit-model qwen/qwen3.6-35b-a3b \
    --rounds 1 --runners 1 --train-size 3 \
    --skip-collusion-check          # only when actor and critic share a family

kent wake-up --duration 5m          # run recall self-improvement games against the live palace
```

`--examples-dir DIR` loads real training examples (one JSONL per file, line shape `{task_id, prompt, ...}`). Without it, synthetic prompts are used (smoke-test only).

### Test layout (31 total, in `tests/training/`)

| File | Tests | What it proves |
|---|---|---|
| `test_palace_isolation.py` | 7 | Snapshot/cleanup, SQLite copy branch, diary copy branch (no hardlinks), parallel rollout safety |
| `test_critic_scorer.py` | 8 | JSON parse, code-fence regex, score clamping to [0,1], scalar-reward weights |
| `test_swap_pair.py` | 5 | Family-collision rejection, cross-product sweep |
| `test_recall_games.py` | 3 | Game A logic against a mocked palace (mempalace API has drifted; covers code path only) |
| `test_rollout.py` | 3 (`integration`) | Rollout end-to-end with FakeLLM; transcript collection regression test for issue #1 |
| `test_apo_e2e.py` | 2 (`live_apo`) | Single rollout against Atlas Qwen; full APO round on `query_rewrite_policy` via Game-A rollouts |
| `test_training_efficacy.py` | 2 (`memory` + `live_apo`) | Embedding similarity responds to query quality; directive-vs-baseline policy A/B against Atlas |

### What's been verified live

| Test | Status | Wall time | Result |
|---|---|---|---|
| `test_recall_metric_responds_to_query_quality` | ✅ green | 1.6s | drawer-aware queries scored avg sim 0.323 vs 0.027 for unrelated; 3/3 pairwise wins |
| `test_rollout_e2e_atlas` | ✅ green | 14.4s | Qwen called `memory_recall`, critic scored 1.000, scratch palace cleaned up |
| `test_apo_train_query_rewrite_policy_atlas` | ⚠️ partial | ~10 min then hangs in shutdown | Round 01 completes (v0=0.866 wins, 4 rollouts at 9-13s each, APO produced edited candidate v1=0.778). Algorithm phase works; AgentOps/SharedMemoryStrategy shutdown hang is upstream. |
| `test_retrieval_policy_ab_against_atlas` | ⏸ wired, not yet run | est. ~3 min | n/a |

### Suggested next tests

The current ladder validates pipeline plumbing and the *training signal* (better queries → better embedding scores). What's not yet proven: that **APO discovers** better prompts, and that the other plan resources (`scope_policy`, `closet_summary_policy`, `actor_system_prompt`) train cleanly. Order by value/effort:

1. **Sequential resource freezing test** (unit, fast). Save a fake optimized `actor_system_prompt.txt` to `lightning_store/resources/`, run a rollout for `retrieval_policy`, assert the frozen actor prompt is concatenated into the system prompt. Exercises plan line 47 directly.
2. **Collusion probe trip-wire** (unit, fast). Mock a critic that scores 5/5 on bad outputs; assert `cmd_train` aborts with the right exit code. Plan line 138 calls it "mandatory" — currently only validated by the eval_harness unit test, not the cmd_train wiring.
3. **Game B scope_eval live test** (`live_apo`, ~3 min). Replay queries at three scopes, assert the critic-picked scope matches the seeded wing. Plan line 26.
4. **Game C closet_fidelity live test** (`live_apo`, ~3 min). Sample a closet, generate question, assert actor can answer from closet alone. Plan line 27. Stratify by drawer source (`transcript` vs `diary`).
5. **Multi-round APO improvement test** (`live_apo`, slow). Run APO with `n_rounds=3` on `query_rewrite_policy` and assert val_reward at round 3 ≥ val_reward at round 1. The first concrete claim that APO actually improves the prompt — currently only ran round 01.
6. **Trained-vs-baseline efficacy** (`live_apo`, slow). Run rollouts with the saved optimized prompt vs the seed, count drawer-content hits in the actor's response. Plan verification step 4.
7. **APO shutdown-hang fix or workaround**. Either a documented `os._exit(0)` after assertions in slow tests, or upstream issue against agentlightning/agentops. Currently blocks CI on `test_apo_train_query_rewrite_policy_atlas`.
8. **Concurrent-rollout stress test** (slow). 10 parallel `kent_task_rollout` calls against the same palace; assert no SQLite or diary corruption. Plan critical risk #4 says n_runners=4 multiplies race risk; we have one tiny test for two parallel rollouts but nothing at scale.
9. **Wing-scoped recall A/B**. Same shape as `test_retrieval_policy_ab_against_atlas` but using `memory_recall_here` to test that wing routing actually narrows results.

## Persistent memory

Kent has long-term, cross-session memory **on by default**. It's powered by [MemPalace](https://github.com/MemPalace/mempalace), a local-first, ChromaDB-backed store that requires no API key and runs entirely on your machine.

There is nothing to enable. The first time you launch `kent`, `kent run`, or call `agent.run(...)` from your own code, a palace is created at `~/.kent/palace` and every conversation turn from that point on is persisted. The next session — same machine, hours or weeks later — recalls relevant context automatically.

Kent owns its own ChromaDB palace at `~/.kent/palace` (configurable via `$KENT_HOME`). It does **not** share the default mempalace location at `~/.mempalace/palace`, so kent's verbatim conversations stay isolated from other mempalace consumers (`mempalace mine`, the MCP server, etc.) on the same machine.

### Default-on behavior

| Entry point | Memory behavior |
|---|---|
| `kent` (REPL) | Constructs `MemPalaceStore()`, injects wake-up at session start, registers the `memory_recall` tool, records every turn |
| `kent run "<prompt>"` | Same as REPL, just one-shot |
| `agent.run(messages=..., tools=..., llm=...)` (library) | Lazily constructs a default `MemPalaceStore` if `memory_store=None` and threads it through the loop and `maybe_compact` |
| Tests | `tests/conftest.py` autouse fixture monkey-patches `_default_store` to a private `NullMemoryStore` so unit tests stay offline |

To **opt out** as a library consumer, pass any object implementing the 3-method `MemoryStore` protocol — for example, a no-op stub:

```python
class NullStore:
    @property
    def session_id(self): return "no-memory"
    def record_turn(self, messages, *, session_id): pass
    def wake_up(self): return ""
    def recall(self, query, k=5): return ""

async for ev in run(messages=[...], tools=registry, llm=llm, memory_store=NullStore()):
    ...
```

### What we use from MemPalace

Kent uses a deliberately small surface of mempalace's API. The full library ships 29 MCP tools, a CLI, and four memory layers; we reach into three submodules and ignore the rest.

| MemPalace API | Where kent calls it | Purpose |
|---|---|---|
| `mempalace.sweeper.sweep(jsonl_path, palace_path, source_label="kent")` | `MemPalaceStore.record_turn` (every turn) | Ingests the per-session JSONL into ChromaDB at `~/.kent/palace`. Idempotent — drawer IDs are deterministic, so re-sweeping the same file is a no-op. |
| `mempalace.layers.MemoryStack(palace_path).wake_up()` | Session start (REPL, `kent run`) and inside `maybe_compact` | Returns ~600–900 tokens of L0 (identity) + L1 (essential moments) — short enough to inject into a system message every compaction without bloat |
| `mempalace.layers.MemoryStack(palace_path).status()` | `kent doctor` `[memory]` block | Reports `total_drawers` for the health check |
| `mempalace.layers.Layer3(palace_path).search(query, n_results=k)` | `memory_recall` tool, `/recall` slash command | Deep semantic search over all drawers. We use `Layer3.search` rather than `searcher.search` because the latter prints to stdout instead of returning. |
| `mempalace.sweeper.parse_claude_jsonl(path)` | `tests/test_memory_transcript.py` only | Used to verify that our transcript writer produces JSONL conformant with mempalace's reader |

**Sweeper-ingested turns carry no `wing` metadata.** Mempalace's `sweeper.sweep()` does not write a `wing` field to drawer metadata — only `diary_ingest` and a few other paths set wings. Kent achieves palace isolation by owning a separate ChromaDB at `~/.kent/palace` instead of sharing `~/.mempalace/palace` with other tools. Wings are used exclusively for the diary path (see [Wings & diary](#wings--diary)).

What we **don't** use from MemPalace on the sweeper path:
- **`mempalace.convo_miner.mine_convos`** — batch-import for an existing corpus. Kent streams live via `sweep` per turn.
- **The 29 MCP tools** — kent isn't an MCP host; mempalace is used as a Python library.
- **Direct ChromaDB writes** — `sweep` handles dedup. We never reach into the chromadb collection ourselves.

### How a turn flows through MemPalace

```
   user types "remember my favorite color is octarine"
              │
              ▼
   ┌───────────────────────────────────────┐
   │ agent.run(...) → loop.py turn         │
   │   model streams; tools run; assistant │
   │   message + tool results form a turn  │
   └───────────────────────────────────────┘
              │  end-of-turn (any terminal: completed, max_turns,
              │   tool_loop, model_error, context_overflow, aborted)
              ▼
   MemPalaceStore.record_turn(messages, session_id=…)
              │
              ▼
   ┌─────────────────────────────────────────────────────┐
   │ 1. append_messages → JSONL line per message in      │
   │    Claude-Code format at                            │
   │    ~/.cache/kent/transcripts/<session_id>.jsonl     │
   │    • role: user / assistant / tool_use / tool_result│
   │    • sessionId, uuid, timestamp, content            │
   └─────────────────────────────────────────────────────┘
              │
              ▼
   ┌─────────────────────────────────────────────────────┐
   │ 2. mempalace.sweeper.sweep(...)                     │
   │    • parses JSONL                                   │
   │    • generates deterministic drawer IDs             │
   │      `sweep_<session_id>_<message_uuid>`            │
   │    • upserts into ChromaDB at ~/.kent/palace        │
   │    • idempotent: re-sweep is a no-op                │
   └─────────────────────────────────────────────────────┘
```

On the **read side**, two paths surface stored memory back to the model:

1. **Wake-up injection** (proactive). At session start the REPL/CLI calls `MemoryStack.wake_up()`, wraps the result in `<recalled-memory>…</recalled-memory>`, and prepends it as a system message. When `maybe_compact` fires mid-session, the same wake-up text is embedded inline in the new summary message — so the priming refreshes after every compaction instead of being lost when the head is summarized away.

2. **`memory_recall` tool** (on-demand). The model can call `memory_recall(query, k=5)` whenever a question references prior context. This routes to `Layer3.search`, which does semantic vector search over all drawers and returns formatted text the model can quote back.

The two paths are complementary: wake-up gives the model passive priming with the most "essential" L1 moments; `memory_recall` gives it active retrieval for specific queries that wake-up didn't surface.

### Wings & diary

Kent supports **wings** — named project/intent contexts — and a per-wing **agent diary** that captures the model's observations, findings, decisions, and recurring patterns across sessions.

#### Filesystem layout

All wing state lives under `${KENT_HOME}` (default `~/.kent/`):

```
~/.kent/
├── palace/                     # ChromaDB — conversation turns (sweeper path)
├── active_wing.txt             # one line: current wing name
└── diaries/
    ├── kent_default/
    │   ├── .intent.txt         # one-line wing description
    │   ├── 2026-04-27.md       # today's diary entries
    │   └── 2026-04-28.md
    └── prod-deploys/
        ├── .intent.txt
        └── 2026-04-27.md
```

The directory layout *is* the wing registry. `list_wings()` = `ls ~/.kent/diaries/`. No separate registry file.

#### Wing creation flow

Wings are created on demand. When the model encounters a new project intent:

1. Model calls `set_wing(name="prod-deploys")` — no wing exists yet → returns an error telling the model to ask the user for a one-line description and re-call.
2. User confirms; model calls `set_wing(name="prod-deploys", intent="monitor terraform deploy pipeline")`.
3. Wing directory and `.intent.txt` are written; store switches to that wing.

You can also switch wings directly from the REPL:

```
/wing prod-deploys        # switch (must exist)
/wings                    # list all wings with intents
/wing                     # show current active wing
kent --wing prod-deploys  # set wing for the whole session
```

#### Writing diary entries

```
/diary the build slowed 30% after midnight
```

…or the model calls `diary_write(kind="OBSERVATION", text="...", topic="builds")`.

Valid kinds: `OBSERVATION`, `FINDING`, `DECISION`, `PATTERN`.

Entries are appended to `~/.kent/diaries/<wing>/YYYY-MM-DD.md` under `fcntl.flock` and immediately ingested into ChromaDB via `mempalace.diary_ingest.ingest_diaries`. The format matches the diary spec from mempalace:

```markdown
# 2026-04-27

## 14:32:01 [agent=kent] [OBSERVATION] builds
The build pipeline got 30% slower after midnight.

## 15:08:44 [agent=kent] [DECISION] feature-flags
Decided to gate the new ranker behind FF_RANKER_V2.
```

#### Recalling diary entries

Two surfaces:

| Surface | When to use |
|---|---|
| `memory_recall_here(query)` / `/recall-here <q>` | Active search — semantic lookup in current wing's diary |
| Session wake-up (automatic) | Passive priming — `wake_up_full()` injects both global L0+L1 AND recent wing diary content at session start |

**Wing-scoped recall is diary-only.** Turn transcripts ingested by `sweeper.sweep()` carry no `wing` metadata, so `memory_recall_here` only surfaces diary entries (written via `diary_write`). Use `memory_recall` for cross-session turn retrieval.

#### Session start vs. compaction

| Event | Which wake-up | Why |
|---|---|---|
| Session start (`kent`, `kent run`) | `wake_up_full()` — global + wing diary | Fresh session benefits from full context |
| Compaction (`maybe_compact`) | `wake_up()` — global only | Saves tokens on every mid-session compaction; diary still reachable via `memory_recall_here` |

#### Caveats

- **Diary is append-only.** Editing requires manually modifying the `.md` file and re-running ingest with `force=True`. `/forget` only removes the current session's turn transcript — diary entries persist.
- **Wing rename/delete not supported** in v1. Renaming orphans drawers (different `(wing, date)` hash in ChromaDB). Work around with `rm -rf ~/.kent/diaries/<old>` + manual ChromaDB cleanup.
- **Secrets caveat applies to diaries too.** Anything written to a diary entry is stored verbatim in ChromaDB. Use `mempalace` tools or `rm -rf ~/.kent/palace` to wipe.
- **`~/.mempalace/state/` side effect.** `ingest_diaries` writes a small state file under `~/.mempalace/state/` (hard-coded inside mempalace). The file is SHA-keyed by `(palace_path, diary_dir)` so no collision is possible with other mempalace tools. You can delete it freely.
- **Subagents inherit the active wing.** When `spawn_subagent` is called, the subagent shares the parent's `MemPalaceStore` and therefore the same `active_wing`. Wing mutations by a subagent via `set_wing` affect the parent's state on the next turn.

### Crash safety and error handling

- **Recording fires on every terminal reason.** The loop calls `record_turn` on `completed`, `next_turn`, `model_error`, `max_turns`, `tool_loop`, `aborted`, and `context_overflow`. A crashed turn is captured up to the last completed message, not lost.
- **Backend errors never break a conversation.** All three `MemPalaceStore` methods (`record_turn`, `wake_up`, `recall`) are wrapped in `try/except` with `logging.warning`. A broken palace, a chromadb upgrade glitch, or a corrupt drawer surfaces as a warning in the log; the loop carries on.
- **JSONL is a write buffer, not the source of truth.** Per-session JSONL files at `~/.cache/kent/transcripts/` are append-only and accumulate. The durable store is the ChromaDB palace. You can wipe the transcript dir at any time without losing memory.

### Slash commands (REPL)

| Command | Description |
|---|---|
| `/memory` | Show palace path, transcript path, and current session ID |
| `/recall <query>` | Run `Layer3.search` and print the raw results |
| `/forget` | Delete the **current session's** transcript file (with confirmation). Note: long-term palace drawers persist — this only clears the un-swept buffer. |

### Health check

`kent doctor` includes a `[memory]` block:

```
[memory]
  palace     : /Users/you/.kent/palace  (exists: True)
  transcripts: /Users/you/.cache/kent/transcripts  (exists: True)
  drawers    : 1247
  last-write : 2026-04-27T09:04:22
```

`drawers` comes from `MemoryStack.status()`; `last-write` is the most recent mtime among palace files. Both are `0` / `<never>` until the first turn is recorded.

### Caveats

- **Secrets are stored verbatim.** Anything echoed in a conversation — API keys leaked through `shell` output, `.env` contents read by a tool, password fragments — ends up in the palace as searchable text. `/forget` removes the current session's JSONL buffer, but already-swept drawers persist; use mempalace's own tools to clear them, or `rm -rf ~/.kent/palace` to wipe everything.
- **First import is heavy.** MemPalace pulls in chromadb (~300 MB) and downloads a ~80 MB ONNX model on first use for embeddings. Kent lazy-imports mempalace inside `MemPalaceStore.__init__`, so `import agent` itself stays light — the cost is paid on first `record_turn` / `wake_up` call.
- **Subagents share the parent's store.** When `kent` spawns a subagent via `spawn_subagent`, it threads the same `MemPalaceStore` through. Concurrent sibling-spawn writes are unverified — see Known limitations.
- **No per-project scoping.** Kent uses one palace at `~/.kent/palace` for everything. If you want per-project isolation today, set `$KENT_HOME` to a project-specific directory before launching.

---

## Known limitations

- No built-in retries or rate limiting — wrap `run()` yourself if needed.
- No timeouts on tool calls — use `signal` for cancellation (the `shell` tool has its own per-command timeout).
- No Anthropic-native API — use a litellm proxy or `OpenAICompatibleLLM` with an OpenAI-format endpoint.
- No live integration tests in CI — run `tests/integration/` manually with `OLLAMA_HOST` set.
- DuckDuckGo HTML can rate-limit aggressive use; `web_search` is best-effort scraping, not a contracted API.
- **Concurrent subagent memory writes are unverified.** When a `Spawn`-ed subagent shares the parent's `MemPalaceStore`, sibling subagents writing to the same JSONL transcript and ChromaDB upsert path concurrently may race. Safe today because subagents typically serialize their own LLM calls; revisit if you parallelize many spawns.
- **Transcript buffer grows unbounded.** Per-session JSONL files under `~/.cache/kent/transcripts/` are never pruned. They are a write buffer; the durable store is ChromaDB. Sweep / delete the directory yourself if it grows large.
