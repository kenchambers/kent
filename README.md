# kent

A minimal, typed async agent runtime for OpenAI-compatible LLMs, plus a `kent` CLI for using it interactively from the terminal — in the spirit of [opencode](https://opencode.ai/) and [hermes-agent](https://hermes-agent.nousresearch.com/), but small enough to read in one sitting.

The Python package is imported as `slim_agent`; the installed CLI binary is `kent`.

## Table of contents

- [What this is](#what-this-is)
- [Repo layout](#repo-layout)
- [Install](#install)
- [Getting started](#getting-started)
  - [1. Set your API key](#1-set-your-api-key)
  - [2. Launch the REPL](#2-launch-the-repl)
  - [3. One-shot mode](#3-one-shot-mode)
- [CLI reference](#cli-reference)
  - [`kent`](#kent-1)
  - [`kent run`](#kent-run)
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
- [Known limitations](#known-limitations)

## What this is

Two layers in one repo:

1. **A library** (`slim_agent`) — a ~400-line agent loop that streams events, starts safe tool calls *while the model is still streaming*, partitions concurrent vs. serial tools, and recovers from context-window overflow by compacting and retrying. Works against anything OpenAI-shaped: OpenAI, Atlas Cloud, Together, Groq, OpenRouter, vLLM, Ollama, llama.cpp.
2. **A CLI** (`kent`) — a small terminal front-end that auto-detects your shell, prompts for a service / model / key on first run, persists the choice, and drops you into a REPL with web-search, web-fetch, shell, and subagent tools wired up. Ships subcommands (`run`, `auth`, `models`, `doctor`) so it's scriptable too.

Web search uses **DuckDuckGo HTML scraping** — no third-party search API key required.

## Repo layout

```
slim_agent/
├── __init__.py        # public exports
├── cli.py             # `kent` CLI: subcommands, REPL, slash commands, persistence
├── loop.py            # the agent loop (streams events, drives tools, handles overflow)
├── llm.py             # LLM protocol + OpenAICompatibleLLM (driven by openai SDK)
├── tools.py           # Tool protocol, ToolRegistry, StreamingExecutor (concurrent/serial batching)
├── state.py           # immutable LoopState, terminal/transition reasons
├── events.py          # all event dataclasses (TextDelta, ToolCallComplete, Terminal, …)
├── compact.py         # context-window compaction + recovery
└── builtin/
    ├── shell.py       # cross-platform shell tool (bash / wsl / powershell)
    ├── spawn.py       # spawn_subagent: delegate a subtask with its own context window
    ├── web_search.py  # DuckDuckGo HTML scraping (no API key)
    └── web_fetch.py   # URL → markdown via httpx + markdownify

tests/                 # pytest suite (offline + opt-in integration tests)
```

## Install

From PyPI (when published):

```bash
uv add slim-agent
```

From a clone:

```bash
git clone <repo-url> kent
cd kent
uv sync
```

This installs the `kent` binary into the project venv. Either run it via `uv run kent …` or activate the venv (`source .venv/bin/activate`) and use `kent` directly.

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

| Command         | What it does                                |
|-----------------|---------------------------------------------|
| `/help`         | Show the slash command list                 |
| `/tools`        | List registered tools                       |
| `/model`        | Show service / model / context window       |
| `/clear`        | Clear conversation history (keep the session) |
| `/exit`, `/quit`| Leave the session                           |

## Built-in tools

| Tool             | What it does                                                         | API key | Concurrency-safe |
|------------------|----------------------------------------------------------------------|---------|------------------|
| `web_search`     | DuckDuckGo HTML scraping — returns `[{title, url, snippet}]`         | none    | yes              |
| `web_fetch`      | URL → markdown via httpx + markdownify (10 MB cap, 100K char output) | none    | yes              |
| `shell`          | Host shell (bash on macOS/Linux/WSL, PowerShell on Windows)          | none    | no               |
| `spawn_subagent` | Delegate a focused subtask with its own context window               | none    | yes              |

Concurrency-safe tools batch and run in parallel via `StreamingExecutor`; unsafe tools (like `shell`) serialize so they can't race state mutations.

## Supported services

| Service     | Default model              | Base URL                          | Env var                |
|-------------|----------------------------|-----------------------------------|------------------------|
| atlascloud  | `qwen/qwen3.6-35b-a3b`     | `https://api.atlascloud.ai/v1`    | `ATLASCLOUD_API_KEY`   |

Adding a new service: edit `SUPPORTED_SERVICES` in `slim_agent/cli.py` — it's a dict literal. For library use, just instantiate `OpenAICompatibleLLM(base_url=..., api_key=..., model=..., context_window=...)` directly.

## Configuration

Files live under `~/.kent/` (override with `KENT_HOME=/some/path`):

| File                              | Contents                              | Notes                          |
|-----------------------------------|---------------------------------------|--------------------------------|
| `~/.kent/config.json`             | `{service_id, model}`                 | Non-secret; written on first run |
| `~/.kent/credentials.json`        | `{<service_id>: <api_key>, …}`        | Written by `kent auth`; chmod 0600 |

Override with environment:

| Variable               | Effect                                                       |
|------------------------|--------------------------------------------------------------|
| `KENT_HOME`            | Use a different config dir (default `~/.kent`)               |
| `ATLASCLOUD_API_KEY`   | Atlas Cloud API key — wins over saved credential             |

## Library use

### Minimal example

```python
import asyncio
from pydantic import BaseModel
from slim_agent import run, ToolRegistry, ToolResult, OpenAICompatibleLLM, TextDelta, Terminal

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
from slim_agent import ToolResult, ToolContext

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
from slim_agent import ToolRegistry, OpenAICompatibleLLM
from slim_agent.builtin.spawn import Spawn

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
uv run pytest -m "not integration"   # offline suite (default)
uv run pytest                        # everything (skips integration unless flagged)
uv run pytest tests/integration/     # live: requires OLLAMA_HOST or similar
```

The offline suite covers the agent loop, the streaming executor, compaction, the `Spawn` subagent, and every built-in tool (DDG redirect unwrap, URL validation, html→md conversion, output truncation, shell detection / run / abort / timeout). 71 tests at last count, all green.

## Known limitations

- No built-in retries or rate limiting — wrap `run()` yourself if needed.
- No timeouts on tool calls — use `signal` for cancellation (the `shell` tool has its own per-command timeout).
- No Anthropic-native API — use a litellm proxy or `OpenAICompatibleLLM` with an OpenAI-format endpoint.
- No live integration tests in CI — run `tests/integration/` manually with `OLLAMA_HOST` set.
- DuckDuckGo HTML can rate-limit aggressive use; `web_search` is best-effort scraping, not a contracted API.
