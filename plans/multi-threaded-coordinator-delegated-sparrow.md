# Kent Background-Spawn Architecture (single-prompt, LLM-decided coordination)

## Context

Today, kent's main agent loop (`agent/loop.py`) blocks the user's perspective:
- The REPL (`agent/cli.py:898-925`) calls `input()` (line 900, blocking), then `await _stream_one_turn(...)`. The user can't interact, queue follow-ups, or see partial progress until the entire turn completes.
- The existing `Spawn` tool (`agent/builtin/spawn.py:67`) does `async for ev in agent_run(...)` *inside* the parent's tool-call coroutine — delegation today moves work to a fresh context, but does **not** free the main thread.
- The Discord gateway (`agent/gateway/discord_bot.py:490, 596`) has the same blocking pattern under a per-channel session lock.
- Long-running shell commands run synchronously in the agent's tool call. There's no way to launch a process, return immediately, and read its output later.

We want kent to behave such that every spawn returns immediately and the parent stays interactive. Workers run in `asyncio.create_task`, finish, and post a synthetic `<task-notification>` user-role message that the parent sees on its next turn — the same pattern Anthropic's claude-code uses.

User requirements (confirmed):
1. Works in both **REPL** and **Discord gateway**.
2. Each subagent gets its own `session_id`; all subagents read from the **shared memory palace** after writes complete.
3. **Single prompt, single registry** — every spawned agent gets the **same** system prompt and the **same** tool kit (including `spawn_subagent`). The LLM decides whether to coordinate (recurse via spawn) or just do the work itself. There is no "coordinator mode" code path.
4. **Recursive spawning** — any spawned agent can spawn its own children, with depth + count caps.
5. **Self-destruct** — when a spawned agent's loop terminates, its `BackgroundTask` entry is dropped from the registry as soon as its notification is consumed by the parent's next turn. Python GC reclaims the rest. No manual cleanup paths.
6. **Background shells** — an agent can launch shells (`shell_spawn`), read accumulated output (`task_status`), and kill them (`task_stop`).

Intended outcome: typing a task at the kent prompt prints `Launched: <task-id>` (one line per spawned worker), the prompt returns immediately, and notifications arrive when each worker completes. Multiple workers run concurrently. Workers that decide they're complex enough can spawn their own workers. The top-level REPL/Discord turn never blocks on a worker.

---

## What We Trimmed (was over-engineered in v1 of the plan)

The previous draft sprawled to ~30-40% forward-looking abstraction with no current caller. Cuts:

| Cut | Reason |
|---|---|
| Separate `coordinator_prompt.py` + `worker_prompt.py` | One prompt for everyone. The LLM decides whether to delegate by reading the prompt's "if your task has independent subtasks, use `spawn_subagent`" line. |
| Separate `build_coordinator_registry` + `build_worker_registry` | One augmented registry. Today's `build_registry()` (`cli.py:372-377`) already exists; we extend it with three orchestration tools. Same kit for top-level + every worker — recursion is enabled by simply not removing `spawn_subagent` from the sub-registry, which is a 3-line change in `spawn.py`. |
| `monitor` shell kind | Same code path as `shell_spawn`, only marketing prompt differs. A monitored process is just a `shell_spawn` the agent never asks to kill. Drop the kind entirely. |
| `send_message_to_task` | No current caller. Defer until we actually need mid-flight injection into a running worker. |
| Per-shell log files under `~/.kent/shells/` | In-memory ring buffer (64KB cap, same idea as `_truncate` in `agent/builtin/shell.py:82-87`). Avoids fs cleanup, file descriptors, race-on-rotate. If we ever need persistence, add it later. |
| Standalone `agent/orchestration/abort_cascade.py` | Replaced by a 5-line helper inlined in `agent/orchestration.py`. |
| Standalone `shell_read.py`, `shell_kill.py`, `list_tasks.py` files | Collapsed: `task_status` lists tasks **and** returns recent shell output (uniform interface). `task_stop` kills any task (agent or shell, with cascade). Two tools, two files. |
| `agent/orchestration/` package (5 modules) | Single `agent/orchestration.py` module — `BackgroundTask`, `REGISTRY`, `INBOX`, `build_notification`, `link_child_abort`. ~150 lines total. |

Net: 4 new source files (down from ~12), 5 modified files (about the same).

---

## Single-Prompt, Single-Loop Principle

`agent.loop.run` (`agent/loop.py:59-228`) is **already** a generic async generator. It accepts `messages`, `tools`, `llm`, `system`, optional `signal: asyncio.Event`, and `memory_store`. It works for any caller. This is exactly claude-code's pattern.

Every kent agent — top-level REPL turn, Discord turn, every spawned subagent — invokes the **same `agent.loop.run`** with the **same system prompt** and the **same tool registry**. The only difference between top-level and a worker is:
- Top-level: awaited inline.
- Worker: launched via `asyncio.create_task` from inside `Spawn.call()`, which returns immediately to the parent.

There is no "coordinator" code path. There is no "worker" code path. There is one agent. The LLM, reading the system prompt, decides per-turn whether to:
- call `spawn_subagent` to delegate independent subtasks (it will get back `<task-notification>` messages later), or
- call regular tools (`shell`, `web_search`, `web_fetch`, `memory_recall`, …) to do the work itself.

The system prompt teaches this. The architecture enforces nothing — a smart model recurses when warranted; a focused leaf just gets its task done. This naturally implements the "regular agent that completes start-to-finish then self-destructs" requirement: the leaf's `_drive_worker()` coroutine returns, the registry drops its entry on next inbox drain, Python reclaims everything.

This means `agent/loop.py` itself needs **near-zero new code paths**: just plumbing four optional kwargs (`parent_session_id`, `current_task_id`, `depth`, `parent_abort_event`) into `ToolContext` so `spawn_subagent` can build child task IDs and link abort events.

---

## Architecture Overview

```
User input ──▶ agent.loop.run (default prompt + registry, parent_session_id="repl")
                     │
                     ├─▶ spawn_subagent ── asyncio.create_task( agent.loop.run )  (same prompt + registry)
                     │                              │
                     │                              ├─▶ spawn_subagent (recursive — depth-bounded)
                     │                              ├─▶ shell_spawn ── asyncio.create_subprocess_exec
                     │                              │                       └─ output → in-memory ring buffer
                     │                              └─▶ task_status / task_stop (any task type)
                     ▼
              [returns to user prompt]
                                                    │
                                On loop Terminal ──▶ INBOX.push("repl", <task-notification>)
                                                    │
Next REPL turn ◀── drain inbox ────────────────────┘
                   (and drop registry entries that the parent has now seen)
```

Two task kinds share one registry:
- **agent** — a spawned subagent (worker) running `agent.loop.run` in the background.
- **shell** — a spawned subprocess running until exit or kill, output captured in a ring buffer.

(No `monitor` kind. A long-running shell is just a shell the agent doesn't kill.)

---

## Files to Create

### `agent/orchestration.py` (new — single module, ~150 lines)

Contains everything orchestration-related that's small enough to live together. Slim shapes:

```python
TaskKind = Literal["agent", "shell"]
TaskStatus = Literal["running", "completed", "failed", "killed"]

@dataclass
class BackgroundTask:
    task_id: str                    # "t-<8 hex>" (agent) | "s-<8 hex>" (shell)
    kind: TaskKind
    parent_session_id: str          # "repl" | f"discord:{channel_id}" | f"...:{task_id}" for nested
    parent_task_id: str | None      # None = top-level; used for cascade aborts
    depth: int                      # 0 = top-level; +1 per spawn
    description: str
    status: TaskStatus
    abort_event: asyncio.Event
    aio_task: asyncio.Task          # the asyncio.Task driving execution (agent or shell)
    started_at: float
    ended_at: float | None
    result: str | None              # final assistant text (agent) or last-N stdout/stderr (shell)
    error: str | None
    output_buffer: collections.deque[str] | None  # shells only; bounded ring buffer (64KB)

class BackgroundTaskRegistry:
    """Process-global. Reuses asyncio.Event + asyncio.create_task patterns
    already established in agent/tools.py:12, 157-203 and agent/gateway/heartbeat.py:85."""
    _tasks: dict[str, BackgroundTask]
    _children: dict[str, set[str]]
    def register(self, task: BackgroundTask) -> None: ...
    def get(self, task_id: str) -> BackgroundTask | None: ...
    def list_for_session(self, parent_session_id: str) -> list[BackgroundTask]: ...
    def list_descendants(self, task_id: str) -> list[BackgroundTask]: ...
    def count_running_for(self, parent_session_id: str) -> int: ...
    def kill(self, task_id: str, *, cascade: bool = True) -> int: ...
    def mark_done(self, task_id: str, *, status, result=None, error=None) -> None: ...
    def drop(self, task_id: str) -> None: ...   # called after notification is consumed
REGISTRY = BackgroundTaskRegistry()

class Inbox:
    """Per-session synthetic <task-notification> user-message queue."""
    _queues: dict[str, asyncio.Queue[Message]]
    def push(self, parent_session_id: str, msg: Message, *, task_id: str | None = None) -> None: ...
    def drain(self, parent_session_id: str) -> tuple[list[Message], list[str]]:
        """Returns (messages_to_prepend, task_ids_to_drop)."""
    async def wait_for_any(self, parent_session_id: str) -> None: ...
    def has_pending(self, parent_session_id: str) -> bool: ...
INBOX = Inbox()

def build_notification(task: BackgroundTask) -> Message:
    """<task-notification> XML — exact shape from
    claude-code/coordinator/coordinatorMode.ts:147-160. Used by both agent + shell."""
    body = f"""<task-notification>
<task-id>{task.task_id}</task-id>
<kind>{task.kind}</kind>
<status>{task.status}</status>
<summary>{escape(task.description)}</summary>
<result>{escape(task.result or "")}</result>
<duration_ms>{int((task.ended_at - task.started_at) * 1000)}</duration_ms>
</task-notification>"""
    return {"role": "user", "content": body}

def link_child_abort(parent: asyncio.Event, child: asyncio.Event) -> asyncio.Task:
    """If parent's abort_event fires, set child's. Inline in this module — too small for its own file."""
    async def _watch():
        await parent.wait()
        child.set()
    return asyncio.create_task(_watch())
```

**Self-destruct mechanic** (the requirement): `Inbox.drain()` returns the messages **and** a list of `task_ids` that just delivered notifications. The REPL/Discord caller then calls `REGISTRY.drop(task_id)` for each. After that, the only remaining reference to a completed worker's context is whatever the GC has not yet reclaimed — which is nothing, because the `_drive_worker()` coroutine has already returned.

### `agent/builtin/shell_spawn.py` (new)

```python
class ShellSpawnArgs(BaseModel):
    command: str
    description: str = ""
    timeout_s: int | None = 600   # None = no timeout (long-running)
```

Reuses `_build_shell_executor()` (`cli.py:361-369`) and the existing `ShellExecutor.run()` async pattern (`agent/builtin/_executors.py:53-103`). Differences from the foreground `Shell` tool:
- Returns immediately with `<spawned id="s-xxxx">…</spawned>`.
- Captures stdout/stderr into `task.output_buffer` (a `collections.deque` capped at ~64KB by trimming oldest entries — same trick as the existing `_truncate` in `shell.py:82-87`).
- Wraps the await in `asyncio.create_task` and registers a `BackgroundTask` with `kind="shell"`.
- On exit (or abort), `REGISTRY.mark_done(...)` then `INBOX.push(...)` with the last ~2KB of output as `<result>`.
- Honors abort via the standard `asyncio.Event` (linked to parent's via `link_child_abort`).

No log file. No tail. No monitor mode.

### `agent/builtin/task_status.py` (new)

```python
class TaskStatusArgs(BaseModel):
    task_id: str | None = None    # None → list everything in current session
    tail_bytes: int = 4_000        # for shells: how much recent output to include
```

Behavior:
- `task_id=None`: returns a compact JSON list of every task in the caller's session — `task_id`, `kind`, `status`, `description`, `started_at`, `ended_at`.
- `task_id=specific`: returns full state including (for shells) the last `tail_bytes` of accumulated output.

This single tool covers `list_tasks`, `shell_read`, and "is task X done yet?" — replaces three planned tools with one.

### `agent/builtin/task_stop.py` (new)

```python
class TaskStopArgs(BaseModel):
    task_id: str
    cascade: bool = True   # default-on; opt out only if you really want to leave children running
```

Calls `REGISTRY.kill(task_id, cascade=cascade)`. Sets the `abort_event`. For shells, the existing `ShellExecutor.run()` already races `comm_task` against `ctx.signal` (`_executors.py:64-95`) and `proc.kill()`s on abort. For agent tasks, the worker's `agent.loop.run` already honors `signal` (`loop.py:162-169, 189-194`). Cascade walks `REGISTRY.list_descendants(task_id)` and sets each.

Replaces `shell_kill.py` + `task_stop.py` with one tool.

---

## Files to Modify

### `agent/builtin/spawn.py` — non-blocking + recursion-enabled

Currently (lines 50-82) blocks on the full sub-loop. New behavior:

```python
MAX_SPAWN_DEPTH = 5            # configurable via KENT_MAX_SPAWN_DEPTH
MAX_TASKS_PER_SESSION = 32     # registry cap to prevent runaway fanout

class SpawnArgs(BaseModel):
    instructions: str
    description: str = ""           # short label for registry/notifications
    tools: list[str] | None = None  # default = same registry as parent (recursion enabled)

async def call(self, args: SpawnArgs, ctx: ToolContext) -> ToolResult:
    if ctx.depth >= MAX_SPAWN_DEPTH:
        return ToolResult(call_id="", output=f"<error>spawn rejected: depth {MAX_SPAWN_DEPTH} reached</error>", is_error=True)
    if REGISTRY.count_running_for(ctx.parent_session_id) >= MAX_TASKS_PER_SESSION:
        return ToolResult(call_id="", output="<error>spawn rejected: too many running tasks</error>", is_error=True)

    task_id = f"t-{secrets.token_hex(4)}"
    sub_session_id = f"{ctx.parent_session_id}:{task_id}"
    sub_store = self.memory_store.fork(session_id=sub_session_id) if self.memory_store else None

    # Same registry as parent. Filtering only when explicitly requested.
    sub_tools = self.parent_registry if args.tools is None \
                else _filter_tools(self.parent_registry, args.tools)

    abort = asyncio.Event()
    if ctx.parent_abort_event is not None:
        link_child_abort(ctx.parent_abort_event, abort)

    async def _drive_worker():
        final_text = ""
        try:
            async for ev in agent_run(
                messages=[{"role": "user", "content": args.instructions}],
                tools=sub_tools,
                llm=self.llm,
                system=self.system_prompt,    # SAME prompt as parent
                max_turns=15,
                signal=abort,
                memory_store=sub_store,
                parent_session_id=sub_session_id,
                current_task_id=task_id,
                depth=ctx.depth + 1,
                parent_abort_event=abort,
            ):
                if isinstance(ev, AssistantMessageComplete) and ev.message.content:
                    final_text = ev.message.content
                if isinstance(ev, Terminal):
                    status = "killed" if abort.is_set() else _status_from(ev.reason)
                    REGISTRY.mark_done(task_id, status=status, result=final_text)
                    INBOX.push(ctx.parent_session_id, build_notification(REGISTRY.get(task_id)), task_id=task_id)
                    return
        except Exception as e:
            REGISTRY.mark_done(task_id, status="failed", error=str(e))
            INBOX.push(ctx.parent_session_id, build_notification(REGISTRY.get(task_id)), task_id=task_id)

    aio_task = asyncio.create_task(_drive_worker())
    REGISTRY.register(BackgroundTask(
        task_id=task_id, kind="agent",
        parent_session_id=ctx.parent_session_id, parent_task_id=ctx.current_task_id,
        depth=ctx.depth + 1, description=args.description or args.instructions[:80],
        status="running", abort_event=abort, aio_task=aio_task,
        started_at=time.time(), ended_at=None, result=None, error=None,
        output_buffer=None,
    ))
    return ToolResult(call_id="", output=f"<spawned id='{task_id}'>{args.description or args.instructions[:80]}</spawned>")
```

Constructor change: `Spawn` now also accepts the system prompt so the worker uses the same one as the parent:
```python
def __init__(self, *, parent_registry, llm, memory_store=None, system_prompt: str):
    ...
```

Key points:
- Workers reuse `agent.loop.run`. Same generator. Same system prompt. Same tools.
- `tools=None` means "give the worker exactly what I have" — including `spawn_subagent`. This **enables recursive delegation** by removing the existing exclusion logic at `spawn.py:54-62` that today filters out `spawn_subagent`. The depth + count caps are the safety net.
- The existing tests at `tests/test_spawn.py:220-242` (`test_subagent_no_recursive_spawn`) and `tests/test_spawn.py:245-280` (`test_spawn_default_tools_excludes_spawn`) need to be updated/replaced — recursive spawning is now intentional.

### `agent/tools.py` — extend `ToolContext`

```python
class ToolContext:
    def __init__(self, *,
        signal: asyncio.Event | None = None,
        expose_tool_errors: bool = False,
        parent_session_id: str = "unknown",
        current_task_id: str | None = None,
        depth: int = 0,
        parent_abort_event: asyncio.Event | None = None,
    ):
        ...
```

`StreamingExecutor.__init__` already constructs `ToolContext` (`tools.py:162`). Pass the new kwargs through.

### `agent/loop.py` — accept context propagation

Add four optional kwargs to `run(...)` (line 59) and forward them when constructing `StreamingExecutor` (line 117):
```python
async def run(*, ..., parent_session_id: str = "unknown",
              current_task_id: str | None = None,
              depth: int = 0,
              parent_abort_event: asyncio.Event | None = None) -> AsyncGenerator[...]:
    ...
    executor = StreamingExecutor(
        tools, can_use_tool, signal, expose_tool_errors,
        parent_session_id=parent_session_id, current_task_id=current_task_id,
        depth=depth, parent_abort_event=parent_abort_event,
    )
```

Zero changes to control flow. Pure plumbing.

### `agent/builtin/__init__.py` (or `cli.py:build_registry`) — register orchestration tools

The simplest path is to modify `cli.py:build_registry()` (lines 372-377) and `gateway/discord_bot.py:_build_discord_registry()` (lines 119-158) to also register `ShellSpawn`, `TaskStatus`, `TaskStop` alongside everything else. **One registry per call site, augmented in-place.** No `build_coordinator_registry` / `build_worker_registry` split.

```python
# in cli.py
def build_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(WebSearch())
    r.register(WebFetch())
    r.register(Shell(executor=_build_shell_executor()))
    r.register(ShellSpawn(executor=_build_shell_executor()))   # NEW
    r.register(TaskStatus())                                    # NEW
    r.register(TaskStop())                                      # NEW
    return r
```

Spawn is registered after build_registry returns (existing pattern at `cli.py:831`), now with `system_prompt`:
```python
registry.register(Spawn(parent_registry=registry, llm=llm,
                        memory_store=memory_store, system_prompt=system_prompt))
```

### `agent/cli.py` — REPL drains inbox + async input

In `_repl()` (line 812-943) make four small changes:

1. **Async input** (line 900): `await asyncio.to_thread(input, prompt_str)` — frees the event loop so `_drive_worker()` coroutines and the inbox can keep running.
2. **Drain inbox before each turn** (between line 905 and line 917): pull pending notifications + task IDs to drop, prepend the messages to `history`, and `REGISTRY.drop(...)` each id. This is the self-destruct point.
3. **Idle-time notification surfacing**: while `asyncio.to_thread(input, …)` is blocking on the user, race against `INBOX.wait_for_any("repl")` using `asyncio.wait(return_when=FIRST_COMPLETED)` (already used at `tools.py:202, 251`). On notification fire, print a one-liner (`✓ task t-abc completed: …`) but **don't** consume the notification — leave it for the next turn's drain so the LLM sees it too.
4. **Pass session_id**: `agent.loop.run(parent_session_id="repl", depth=0, ...)` from `_run_once()`.

System prompt: extend `_SYSTEM_PROMPT_BASE` (`cli.py:81-99`) with two short paragraphs:
- "When you call `spawn_subagent`, it returns immediately with a task-id. The worker runs in the background; its result will arrive on a later turn as a `<task-notification>` user message."
- "If the user's request has independent subtasks, prefer to spawn them in parallel. If your spawned worker's task is itself complex, the worker is free to spawn its own children — but stop recursing when the work is small enough to do directly."

That's the entire "coordinator vs worker" instruction — embedded in the one shared prompt.

### `agent/gateway/discord_bot.py` — Discord drains inbox + proactive notifier

Same pattern, parent_session_id = `f"discord:{channel.id}"`:
1. **Drain inbox at turn entry** in `_handle_turn(...)` just before `agent_run(...)` (line 490) and in `_run_heartbeat_turn(...)` (line 596). Drop consumed task ids.
2. **Per-channel proactive dispatcher** — same shape as `agent/gateway/heartbeat.py:84-95`: a per-gateway `asyncio.Task` looping on `INBOX.wait_for_any(channel_session_id)` for each known session. On fire, acquire the channel's `session.lock`, post a one-line `✓ task … completed` (without consuming the notification — next user turn drains it).
3. **Pass session_id + register orchestration tools** in `_build_discord_registry()` (lines 119-158).

### `agent/memory/mempalace_store.py` — `fork()` + write lock

Two changes:

1. **`fork(session_id: str) -> MemPalaceStore`** — returns a sibling store with the new session_id. Same `palace_path`, same `kent_home`. The transcript path is already keyed by session_id (line 44), so this is a small constructor variant:
   ```python
   def fork(self, *, session_id: str) -> "MemPalaceStore":
       sibling = MemPalaceStore(palace_path=self._palace_path, kent_home=self._kent_home)
       sibling._session_id = session_id
       sibling._transcript_path = _TRANSCRIPT_BASE / f"{session_id}.jsonl"
       sibling._active_wing = self._active_wing
       return sibling
   ```

2. **Process-global `asyncio.Lock` around `sweep(...)`** in `record_turn` (line 81). Without it, parallel workers race the palace write. Reads (`recall`, lines 118-125) tolerate stale state and don't need locking.

User requirement check: each subagent has its own session_id ✓; all workers share the post-sweep palace ✓.

---

## Sandbox / Safety Posture

Kent runs on a trusted personal machine. **Full sandboxing is out of scope.** Two minimal guardrails, both enforced inside `Spawn.call()`:

- **Spawn depth cap** (`MAX_SPAWN_DEPTH = 5`) — caps recursive agent spawning.
- **Per-session task cap** (`MAX_TASKS_PER_SESSION = 32`) — caps total concurrent background work per top-level session.

Shell tools inherit kent's existing shell behavior (no additional sandbox).

---

## Critical Files Summary

| File | Action | Reason |
|---|---|---|
| `agent/orchestration.py` | new (single file) | `BackgroundTask`, `REGISTRY`, `INBOX`, `build_notification`, `link_child_abort` — ~150 lines |
| `agent/builtin/spawn.py` | rewrite | Non-blocking, depth-bounded, recursion-enabled, self-cleaning, takes shared system prompt |
| `agent/builtin/shell_spawn.py` | new | Background shell with in-memory ring-buffer output |
| `agent/builtin/task_status.py` | new | List tasks + read shell output (combined) |
| `agent/builtin/task_stop.py` | new | Kill any task type; cascades to descendants |
| `agent/tools.py` | modify | Add `parent_session_id` / `current_task_id` / `depth` / `parent_abort_event` to `ToolContext` |
| `agent/loop.py` | modify | Plumb the four kwargs through `StreamingExecutor` (no control-flow change) |
| `agent/cli.py` | modify | Async input, inbox drain + drop, register orchestration tools, extend system prompt with delegation paragraph |
| `agent/gateway/discord_bot.py` | modify | Inbox drain + drop, per-channel proactive dispatcher, register orchestration tools |
| `agent/memory/mempalace_store.py` | modify | `fork(session_id=…)`, asyncio.Lock around `sweep` |

---

## Reusable Pieces Already in Kent (heavy reuse)

- **`asyncio.Event` abort signal** — `agent/tools.py:12, 157, 198-203`. We literally reuse this Event type for `BackgroundTask.abort_event`. `agent.loop.run` already accepts `signal: asyncio.Event` (loop.py:68) and honors it at lines 162-169 + 189-194 — wire `BackgroundTask.abort_event` directly with no loop changes.
- **`asyncio.wait(return_when=FIRST_COMPLETED)`** — `tools.py:202, 251`. Reused for "race input vs inbox" in REPL and for the per-channel notifier.
- **`asyncio.create_task` + cancel pattern** — `tools.py:168`, `gateway/heartbeat.py:85`. Same shape for `_drive_worker` and the proactive notifier.
- **Shell executor abstraction** — `_build_shell_executor()` at `cli.py:361-369`, `agent/builtin/_executors.py:53-103`. `shell_spawn` calls the same `executor.run()` (or a near-twin that streams to a deque instead of buffering all at once).
- **SIGTERM-then-SIGKILL grace** — `agent/gateway/lifecycle.py:222-251`. The existing `ShellExecutor.run()` already does `proc.kill()` on abort (`_executors.py:77-81`); we don't even need the SIGTERM step at the worker level since signals propagate via `abort_event`.
- **Output truncation** — `agent/builtin/shell.py:82-87` (`_truncate`, 32KB cap). Reuse the same trick for `BackgroundTask.output_buffer` rotation.
- **Per-session memory keying** — `MemPalaceStore.session_id` already keys `record_turn`'s transcript path (`mempalace_store.py:44, 80`). `fork()` is a five-line constructor variant.
- **Discord per-channel session + lock** — `gateway/discord_bot.py:_session_for(...)` (around line 184). The inbox drain slots into `_handle_turn` between line 466 and 490 with no other restructuring.
- **Heartbeat-style background loop** — `gateway/heartbeat.py:84-95` is the template for the per-channel notifier.

---

## Verification

End-to-end checks:

1. **REPL — single delegation**: `kent` → `find all .py files modified in the last day`. Expect immediate `Launched: t-xxxx`, prompt returns < 1s, `✓ task t-xxxx completed: …` arrives later. Worker transcript shows the worker doing the work directly (no inner `spawn_subagent` calls because the LLM judged it simple).

2. **REPL — parallel delegation**: `count python files AND fetch https://example.com`. Two `Launched: t-...` lines back-to-back; both run concurrently; two notifications arrive independently.

3. **REPL — recursive spawning (LLM-decided)**: `summarize each .py file in agent/`. Worker A may decide to spawn N child workers (one per file) and aggregate. Verify `REGISTRY.list_for_session("repl")` shows ≥ 2 levels of depth. Verify the depth cap (5) rejects further spawning. Verify that a worker given a *trivial* task (e.g. `echo hello`) does **not** spawn — it just runs `shell` directly.

4. **REPL — input during long worker**: spawn a slow worker (`run sleep 30 in shell_spawn`). While running, type another message. Prompt accepts immediately; new turn runs; first worker's notification arrives later.

5. **Self-destruct**: spawn a worker, wait for completion, then type any message. After the next turn's inbox drain, `REGISTRY.get("t-xxxx")` returns None. `gc.get_referrers(...)` of the worker's `_drive_worker` coroutine shows nothing in the orchestration module.

6. **Discord — proactive notification**: DM kent `summarize the README`. Reply: `Launched: t-xxxx`. Wait 30s with no further input. Worker's completion arrives as a separate Discord message via the per-channel dispatcher.

7. **Memory sharing**: spawn worker A → "write a diary entry tagged 'multitest'". Wait for completion. Spawn worker B → "search memory for 'multitest'". B's recall finds A's entry.

8. **Background shell — basic**: agent calls `shell_spawn(command="python -u long_script.py", description="run script")`. Returns `s-xxxx` immediately. Agent calls `task_status(task_id="s-xxxx")` repeatedly — sees incremental new output each call. On script exit, notification arrives.

9. **Multiple shells concurrent**: launch 3 shells (`sleep 5 && echo a/b/c`). All `Launched: s-...` return < 1s apart. All three completion notifications arrive within ~5s.

10. **Abort cascade**: spawn worker A which spawns worker B which spawns shell s-xxx. Call `task_stop(task_id="t-A")`. Verify B's status → `killed`, s-xxx exits, all three notifications arrive.

11. **Memory store concurrency**: spawn 5 workers in parallel, each calling `diary_write`. With the asyncio.Lock around `sweep`, no race; palace ends consistent.

12. **Existing tests pass**: `uv run pytest tests/`. The existing `test_subagent_no_recursive_spawn` and `test_spawn_default_tools_excludes_spawn` (`tests/test_spawn.py:220-280`) **must be replaced** — recursive spawning is now intentional, not blocked. New tests: depth-cap rejection at depth 5, count-cap rejection at 32 running tasks, `REGISTRY.drop` actually drops on inbox drain.

13. **Tool gating verified**: trigger any prompt that previously caused kent to call `shell` directly from the top level. The top-level agent should now usually call `spawn_subagent` for substantive tasks (and the worker may then call `shell` itself), or it may call `shell` directly for trivially short commands. The exact split is the LLM's call — we just verify both behaviors are *possible* and that `spawn_subagent` actually runs in the background (transcript timing < 1s for the spawn ToolResult).

---

## Out of Scope (defer)

- `send_message_to_task` mid-flight injection — defer until a real caller exists.
- Persistent shell logs on disk (in-memory ring buffer is enough for v1).
- Worktree-isolated workers.
- Auto-background-after-N-seconds foreground UX.
- Retain/evict cycles for transcript memory.
- SDK progress events.
- Plan-mode interactions for workers.
- Full sandboxing (FS allowlists, network egress filtering, permission elevation prompts) — kent stays trust-based.
