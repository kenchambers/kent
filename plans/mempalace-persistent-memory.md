# Plan: integrate MemPalace as kent's persistent memory

## Context

Today, kent (`slim_agent` runtime + `kent` CLI) loses everything when:
1. `slim_agent.compact.maybe_compact` summarizes the head of the conversation
   (`slim_agent/compact.py:27-46`) — verbatim detail in the head is replaced by
   a single LLM-written summary system message and is gone.
2. The process exits — `_repl()` (`slim_agent/cli.py:401-438`) holds history in
   a local list; `kent run` is one-shot. Nothing is persisted.

The user wants long-term, cross-session memory that survives both events. Library
choice: **MemPalace** (https://github.com/MemPalace/mempalace) — local-first,
ChromaDB-backed, no API key, exposes a usable Python API via `MemoryStack`,
`sweeper.sweep`, and `Layer3.search`. Verbatim storage means the original head
content is never lost.

User decisions (locked from Q&A):

- **Always-on, no opt-out.** `mempalace` becomes a hard dependency of
  `slim-agent`. The agent loop always writes to a memory store; the only way
  to bypass is to pass `memory_store=NullMemoryStore()` (a test seam, not a
  user-facing toggle).
- **Library-default-on.** `slim_agent.run()` constructs a default
  `MemPalaceStore` when no `memory_store` kwarg is supplied. Library users
  importing `slim_agent` get persistence automatically; CLI users get it too.
- **Single global wing** (`wing="kent"`). Every kent session sees every other
  session's wake-up recall. Per-project scoping is deferred.
- **Strategy pattern.** A `MemoryStore` Protocol decouples the loop from
  mempalace. Swapping to a different backend later is a single class.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ slim_agent.run(messages, tools, llm, memory_store=…)               │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ maybe_compact(state, llm, memory_store)                      │  │
│  │   ─ before summarising:                                      │  │
│  │       memory_store.on_compaction(head_messages, session_id)  │  │
│  │   ─ after summarising, append fresh wake_up() recap so       │  │
│  │     long-term recall survives every compaction.              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ at end of every turn (success OR failure terminal):          │  │
│  │   memory_store.on_turn_end(turn_messages, session_id)        │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  Tool registry includes `memory_recall(query, k)` →                │
│      memory_store.search(query, k)                                 │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ slim_agent.memory.MemPalaceStore                                   │
│                                                                    │
│  Persists by *appending Claude-Code-format JSONL* to               │
│  ${KENT_HOME}/transcripts/<session_id>.jsonl, then calls           │
│  mempalace.sweeper.sweep(jsonl_path, palace_path, source_label)    │
│  — idempotent, dedup by deterministic drawer ID.                   │
│                                                                    │
│  wake_up() → mempalace.layers.MemoryStack(palace_path).wake_up(    │
│                  wing="kent")                                      │
│  search(q) → mempalace.layers.Layer3(palace_path).search(          │
│                  q, wing="kent", n_results=k)                      │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
              mempalace ChromaDB at ${MempalaceConfig().palace_path}
              (or override via MemPalaceStore(palace_path=...))
```

The `MemoryStore` Protocol is the swap point. Replacing mempalace later means
implementing the same Protocol; the loop, CLI, and tools don't change.

## The `MemoryStore` Protocol

```python
# slim_agent/memory/store.py
class MemoryStore(Protocol):
    """Strategy interface. Swap implementations without touching the loop."""

    def on_turn_end(
        self, messages: list[Message], *, session_id: str
    ) -> None: ...
    """Called after every turn (any terminal reason) with the new messages
       added this turn (assistant message + tool results). Must be cheap and
       must not raise — backend errors are swallowed and logged."""

    def on_compaction(
        self, head: list[Message], *, session_id: str
    ) -> None: ...
    """Called inside maybe_compact BEFORE the head is replaced by the
       summary. Last chance to persist verbatim detail."""

    def wake_up(self) -> str: ...
    """Return short recap text (≤~1500 tokens) injected as a system message
       at session start AND after every compaction. Empty string if no
       memory yet."""

    def search(self, query: str, k: int = 5) -> str: ...
    """Backing call for the memory_recall tool. Returns model-ready text."""

    def close(self) -> None: ...
    """Flush + release resources at session end."""
```

Two concrete implementations:

- `NullMemoryStore` — no-ops every method, returns `""`. Test seam only.
- `MemPalaceStore` — real impl using mempalace's Python API.

## How `MemPalaceStore` writes

mempalace's natural ingest path is JSONL files consumed by
`mempalace.sweeper.sweep()`. We mirror Claude Code's transcript format exactly,
so we get to reuse `parse_claude_jsonl` and `sweep`'s idempotency / cursor /
dedup machinery for free.

Per session:

1. At `MemPalaceStore.__init__`, generate `session_id = uuid4().hex`. Open
   `${KENT_HOME:-~/.kent}/transcripts/<session_id>.jsonl` for append.
2. `on_turn_end(messages)` writes one JSONL record per message, each with:
   - `type`: `"user"` | `"assistant"`
   - `sessionId`: our session_id
   - `uuid`: `uuid4().hex` per message (stable per write)
   - `timestamp`: ISO 8601 UTC
   - `message`: `{"role": ..., "content": ...}` — for assistant turns, content
     is a list of blocks: `{"type": "text", "text": ...}` plus
     `{"type": "tool_use", "name": ..., "input": ...}` and (in the next
     message slot) `tool_result` blocks. This matches
     `mempalace.sweeper._flatten_content`.
3. After flushing the JSONL append, call
   `mempalace.sweeper.sweep(transcript_path, palace_path, source_label="kent")`.
   The sweeper is idempotent: re-sweeping the same path is a no-op due to
   deterministic `sweep_<session_id>_<message_uuid>` IDs.
4. `on_compaction(head)` writes the head as JSONL (same format) under a
   sentinel session_id `<session_id>_compact_<turn>` so it ingests as a
   distinct chunk and never collides with the live session's drawers.
5. `close()` does one final `sweep()` for safety.

**What we do NOT do:** call `convo_miner.mine_convos` (that's for batch
import of historical exports, not live streaming) or write directly to the
ChromaDB collection (sweeper handles batching, dedup, and the post-1.5.4
hnswlib quirks).

## How `MemPalaceStore` reads

- `wake_up()` → `mempalace.layers.MemoryStack(palace_path=…).wake_up(wing="kent")`
  returns L0 (identity, ~100 tok) + L1 (top-15 essential moments, ≤3200 chars
  ≈ 800 tok). Caller injects as `{"role": "system", "content":
  "<recalled-memory>{text}</recalled-memory>"}`.
- `search(query, k)` → `mempalace.layers.Layer3(palace_path).search(query,
  wing="kent", n_results=k)` returns formatted text suitable for a tool
  result. (We deliberately use `Layer3.search` instead of `searcher.search`
  because `searcher.search` *prints* to stdout rather than returning — see
  `mempalace/searcher.py:218`.)

## When memory hooks fire

| Loop event                                                                     | Hook                              | Why                                                             |
|--------------------------------------------------------------------------------|-----------------------------------|------------------------------------------------------------------|
| `slim_agent/loop.py:74` — top of every turn, just before `maybe_compact`       | (no change)                       | —                                                                |
| `slim_agent/compact.py:40` — after `_summarize`, before replacing `head`       | `memory_store.on_compaction(head)`| Capture verbatim head before it's collapsed into a summary       |
| `slim_agent/compact.py` — after compaction returns                              | re-inject `wake_up()` as system msg | Wake-up text would otherwise be in the compacted head and lost   |
| `slim_agent/loop.py:124` — terminal "completed" path                           | `memory_store.on_turn_end(turn)`  | Existing `on_turn_end` callback already runs here; we layer in   |
| `slim_agent/loop.py:156` — between turns when more turns will follow           | `memory_store.on_turn_end(turn)`  | Same callback; ensures incremental save mid-conversation         |
| **NEW**: terminal paths `model_error`, `max_turns`, `tool_loop`, `aborted`     | `memory_store.on_turn_end(turn)`  | Today these skip `on_turn_end` (data loss risk on crash). Fix.   |
| Tool call: `memory_recall(query, k)`                                           | `memory_store.search(query, k)`   | Model-driven recall                                              |
| REPL start / `kent run` start                                                  | `memory_store.wake_up()` injected | Recall context for new sessions                                  |
| REPL `/exit`, `kent run` end, REPL EOF                                         | `memory_store.close()`            | Final sweep                                                      |

The `model_error`/`max_turns`/`tool_loop`/`aborted` fix is small but important —
without it a context-overflow crash loses everything since the last compaction.
Per "we don't want to lose anything," we move the `on_turn_end` call into a
`finally`-style block in `loop.py` that always runs once per turn.

## Files to change / add

### New files

| Path                                       | Purpose                                                           |
|--------------------------------------------|-------------------------------------------------------------------|
| `slim_agent/memory/__init__.py`            | Re-export `MemoryStore`, `MemPalaceStore`, `NullMemoryStore`      |
| `slim_agent/memory/store.py`               | `MemoryStore` Protocol + `NullMemoryStore`                        |
| `slim_agent/memory/mempalace_store.py`     | `MemPalaceStore` (lazy-imports mempalace at __init__)             |
| `slim_agent/memory/transcript.py`          | JSONL writer producing Claude-Code-compatible records             |
| `slim_agent/builtin/memory_recall.py`      | `MemoryRecall` tool wrapping `MemoryStore.search`                 |
| `tests/test_memory_store.py`               | Protocol conformance, JSONL format, sweep call, hook ordering     |
| `tests/test_memory_transcript.py`          | Round-trip: write JSONL → `parse_claude_jsonl` reads it back      |
| `tests/integration/test_mempalace.py`      | Real mempalace round-trip; gated by `pytest.importorskip`         |

### Changed files

| Path                          | Change                                                                                                     |
|-------------------------------|------------------------------------------------------------------------------------------------------------|
| `slim_agent/loop.py`          | Add `memory_store: MemoryStore \| None = None` kwarg; default-construct `MemPalaceStore()` when None; thread to `maybe_compact`; ensure `on_turn_end` fires on every terminal path; pass `session_id` (auto-generated if not in store). |
| `slim_agent/compact.py`       | Add optional `memory_store` kwarg; call `memory_store.on_compaction(head)` before replacement; append wake-up recap as system message after compaction. Keep 2-arg signature (existing test `test_compact_signature_drops_system_param` still passes). |
| `slim_agent/cli.py`           | `_run_once` / `_stream_one_turn` create a `MemPalaceStore` once per REPL session, pass to `run()`; `_repl()` injects wake-up at start; register `MemoryRecall` tool; new slash commands `/memory`, `/recall <q>`, `/forget` (session-scoped delete with confirmation). |
| `slim_agent/__init__.py`      | Export `MemoryStore`, `MemPalaceStore`, `NullMemoryStore`, `MemoryRecall`.                                |
| `pyproject.toml`              | Add `mempalace>=3.3` to `[project].dependencies` (NOT optional — user requirement).                      |
| `slim_agent/cli.py::cmd_doctor` | Add `[memory]` block: palace_path, drawer count, last-write timestamp, wing="kent" stats.                |
| `README.md`                   | New `## Persistent memory` section.                                                                        |

### Functions/utilities to reuse (no re-implementation)

From the **mempalace** package (verified by reading source on `main`):

- `mempalace.config.MempalaceConfig().palace_path` — default palace dir.
- `mempalace.sweeper.sweep(jsonl_path, palace_path, source_label)` — idempotent
  incremental ingest; returns `{drawers_added, drawers_already_present, …}`.
  Uses deterministic ID `sweep_<session_id>_<message_uuid>`. (`mempalace/sweeper.py:188`)
- `mempalace.sweeper.parse_claude_jsonl(path)` — also useful in our test
  round-trip to confirm our JSONL is mempalace-readable.
- `mempalace.layers.MemoryStack(palace_path).wake_up(wing)` — L0+L1 recap.
  Used by `mempalace/cli.py::cmd_wakeup`.
- `mempalace.layers.Layer3(palace_path).search(query, wing, room, n_results)`
  — returns formatted text.

From the **slim_agent** package:

- `LoopState.advance(**changes)` (`slim_agent/state.py:26`) — for adding the
  re-injected wake-up message after compaction without mutating state.
- `on_turn_end` callback already present in `run()`
  (`slim_agent/loop.py:56`) — repurpose it to invoke `memory_store.on_turn_end`
  in addition to any user callback (chain them).
- `ToolRegistry.register` (`slim_agent/tools.py:41`) — register `MemoryRecall`.
- `Tool` Protocol with `is_concurrency_safe` (`slim_agent/tools.py:17`) — recall
  is read-only, returns True so it batches with `web_search`/`web_fetch`.

## Critical implementation details

1. **Avoiding circular imports.** `slim_agent/loop.py` must not import
   `MemPalaceStore` at top level (mempalace at import time pulls chromadb,
   which is heavy). Use TYPE_CHECKING guards and a `_default_store()` factory
   that constructs lazily inside `run()` if `memory_store is None`.
2. **Errors must not break the loop.** All `MemoryStore` methods are wrapped
   in `try/except` inside the loop with `logging.warning` on failure. A
   broken palace must never break a conversation. (mempalace itself is
   already defensive — sweeper logs and returns `None` cursor on failure.)
3. **JSONL format conformance.** Test must round-trip: write a synthetic
   conversation to JSONL via our writer, then call
   `mempalace.sweeper.parse_claude_jsonl(path)` and assert every record came
   through with `session_id`, `uuid`, `timestamp`, `role`, `content`
   populated.
4. **Tool calls in JSONL.** Assistant messages with tool calls must be encoded
   as content blocks `[{"type": "text", "text": ...}, {"type": "tool_use",
   "name": ..., "input": ...}]`, and tool results as the next message with
   role `"user"` and content `[{"type": "tool_result", "tool_use_id": ...,
   "content": ...}]` — exactly the format `_flatten_content` (sweeper.py)
   expects. Verbatim preservation is the whole point.
5. **Wake-up re-injection guard.** If the wake-up text is empty (no memory
   yet on first run), inject nothing — don't add an empty system message.
6. **Test compatibility.** `test_compact_signature_drops_system_param`
   (`tests/test_compact.py:86`) requires `maybe_compact(state, llm)` works
   with two args. Solution: `memory_store` is a kwarg with default `None`.
7. **No new chromadb writes per token.** The sweeper batches at 64 records;
   one `sweep()` call per turn produces 1–3 batched upserts. Cost per turn
   is ~milliseconds locally.
8. **`session_id` lifecycle.** Generated in `MemPalaceStore.__init__` once.
   `slim_agent.run()` doesn't see it; the store owns it. The transcript file
   is named after it, and every JSONL record carries it as `sessionId`.

## Phased rollout

1. **Phase 1 — Protocol + Null path + transcript writer.** Land
   `MemoryStore`, `NullMemoryStore`, `transcript.py`. Wire `memory_store`
   kwarg through `run()` and `maybe_compact`. Default the loop's store to
   `NullMemoryStore` for now (so existing tests stay green). New tests
   exercise the wiring with a `RecordingMemoryStore` double.
2. **Phase 2 — `MemPalaceStore` + lib default.** Implement `MemPalaceStore`.
   Flip `slim_agent.run()`'s default factory from `NullMemoryStore` to
   `MemPalaceStore()`. Add `mempalace>=3.3` to `pyproject.toml`. Update
   tests that don't want a real palace to inject `NullMemoryStore`
   explicitly.
3. **Phase 3 — Tool + CLI integration.** Register `MemoryRecall` in the
   built-in registry. CLI injects wake-up at REPL start; `_repl()` propagates
   the same `MemPalaceStore` instance across turns; add `/memory`, `/recall`,
   `/forget` slash commands and the `cmd_doctor` block.
4. **Phase 4 — Compaction safety net.** Re-inject wake-up after every
   compaction; widen `on_turn_end` to fire on all terminal paths so a crash
   never loses the in-progress turn.

## Verification

### Unit (offline, default suite)

- `pytest tests/test_memory_store.py` — Protocol conformance: `NullMemoryStore`
  is a no-op; `MemPalaceStore` is constructed with a fake transcript dir +
  monkeypatched `sweep`/`MemoryStack`/`Layer3`; assert `on_turn_end` writes
  one JSONL line per message and calls `sweep` once.
- `pytest tests/test_memory_transcript.py` — write a 4-turn conversation
  (user → assistant w/ tool_use → tool_result → assistant), then read it
  back via `mempalace.sweeper.parse_claude_jsonl` (real import) and assert
  every `(session_id, uuid, role, content)` matches.
- `pytest tests/test_compact.py` — existing tests must still pass (regression
  on the 2-arg signature). Add a new test asserting `on_compaction` fires
  with the head **before** the summary replaces it.
- `pytest tests/test_loop.py` — add cases proving `on_turn_end` fires on
  `model_error` / `max_turns` terminals (using `RecordingMemoryStore`).

### Integration (opt-in, marker `memory` + import skip)

- `pytest tests/integration/test_mempalace.py -m memory` —
  - Construct a real `MemPalaceStore(palace_path=tmp_path)`.
  - Run two synthetic sessions: session A writes "my favorite color is
    octarine"; session B's `wake_up()` text contains "octarine"; session B's
    `search("favorite color")` returns the matching drawer.
  - Force a compaction via a tiny context window; assert `on_compaction`
    fired and the head was sweep-ingested before the summary replaced it.

### Manual smoke

- `kent` REPL, tell it "remember that my favorite color is octarine," `/exit`.
  Re-launch `kent`. Ask "what's my favorite color?" — should recall via
  wake-up or `memory_recall`.
- Long synthetic conversation that triggers compaction; afterward, ask about
  a fact only stated in the head pre-compaction; expect the model to call
  `memory_recall` and get it back.
- `kent doctor` shows the `[memory]` block with non-zero drawer count after
  the smoke runs above.

## Out of scope for this plan

- MCP-server pathway (mempalace ships 29 MCP tools). kent isn't an MCP host.
- Per-subagent wings (the `Spawn` subagent shares the parent's store).
- Per-project / per-cwd wing scoping (locked to single global `wing="kent"`).
- A separate `kent memory` top-level subcommand (slash commands + `doctor`
  cover REPL needs).
- Encryption-at-rest / redaction of secrets in stored drawers (call out as a
  follow-up; document in README that the palace stores verbatim content).

## Risks & mitigations

| Risk                                                                      | Mitigation                                                                                                                       |
|---------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| chromadb install adds ~300 MB and slows first import                      | Lazy-import `mempalace` inside `MemPalaceStore.__init__`. Tests inject `NullMemoryStore`. Document in README under prerequisites. |
| Two `kent` processes racing on the same palace                            | mempalace docs claim Chroma 1.5.4+ tolerates this; if we observe corruption, wrap the per-turn `sweep` in `fcntl.flock` on the palace dir. |
| Wake-up text too large after months of usage                              | mempalace's L1 caps at 3200 chars (≤800 tok). If still too noisy, downgrade to L0-only or a lighter custom recap. Track via `cmd_doctor`. |
| Storing secrets verbatim (API keys echoed in tool output)                 | Document loudly in README; add a future redaction hook in Phase 5. The `/forget` slash command exists for emergency cleanup.    |
| `searcher.search` prints rather than returns                              | We use `Layer3.search` instead — verified to return text (`mempalace/layers.py`).                                             |
| Re-injected wake-up after compaction grows the message list unbounded     | Compact again on next turn if needed; the wake-up replaces the prior wake-up message (track by a sentinel system tag).         |
