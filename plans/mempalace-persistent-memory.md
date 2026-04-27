# Plan: integrate MemPalace as kent's persistent memory

## Context

Today, kent (`agent` runtime + `kent` CLI) loses everything when:

1. `agent.compact.maybe_compact` summarizes the head of the conversation
   (`agent/compact.py:27-46`) — verbatim detail in the head is replaced by
   a single LLM-written summary system message and is gone.
2. The process exits — `_repl()` (`agent/cli.py:401-438`) holds history in
   a local list; `kent run` is one-shot. Nothing is persisted.

The user wants long-term, cross-session memory that survives both events.
Library choice: **MemPalace** (https://github.com/MemPalace/mempalace) —
local-first, ChromaDB-backed, no API key, exposes a usable Python API via
`MemoryStack`, `sweeper.sweep`, and `Layer3.search`. Verbatim storage means
the original head content is never lost.

User-locked decisions:

- **Default-on, no opt-out at the user level.** Library users importing
  `agent.run()` get persistence automatically. Tests opt out via dependency
  injection (a `NullMemoryStore` fixture local to `tests/conftest.py`, not
  shipped in public exports).
- **Single global wing** (`wing="kent"`). Per-project scoping is deferred.
- **Orthogonal interface.** A thin `MemoryStore` Protocol decouples the loop
  from mempalace so an alternative backend can be swapped in later without
  touching the loop, CLI, or tools.

## Verified upstream facts (mempalace `main` as of 2026-04-27)

- `mempalace.sweeper.sweep(jsonl_path, palace_path, source_label=None) -> dict`
  — only ingest entry point. Idempotent: re-sweeping the same JSONL is a
  no-op due to deterministic drawer ID `sweep_<session_id>_<message_uuid>`.
  `BATCH_SIZE = 64`.
- `mempalace.sweeper.parse_claude_jsonl(path) -> Iterator[dict]` — used in
  one of our tests to verify JSONL conformance.
- **No in-memory ingest API exists.** All ingest paths require a JSONL file
  on disk. We work within this constraint with a per-session transient JSONL
  in `${XDG_CACHE_HOME:-~/.cache}/kent/transcripts/` — chromadb is the
  durable store, the JSONL is a write buffer.
- `mempalace.layers.MemoryStack(palace_path).wake_up(wing) -> str` returns
  600–900 tokens of L0 (identity) + L1 (essential moments).
- `mempalace.layers.Layer3(palace_path).search(query, wing, room, n_results) -> str`
  returns formatted text suitable for a tool result.
- `mempalace.config.MempalaceConfig().palace_path` is the default palace dir.
- The package's `__init__.py` only exports `__version__`. We reach in via
  the submodule paths above.

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ agent.run(messages, tools, llm, memory_store=…)                    │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ maybe_compact(state, llm, memory_store=…)                    │  │
│  │   ─ summary message embeds memory.wake_up() text inline,     │  │
│  │     so post-compaction recall priming survives every         │  │
│  │     summarize step. One system message, no scan-and-replace. │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ at end of every turn (any terminal reason):                  │  │
│  │   memory_store.record_turn(turn_messages, session_id=…)      │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  Tool registry includes `memory_recall(query, k)` →                │
│      memory_store.recall(query, k)                                 │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│ agent.memory.MemPalaceStore                                        │
│                                                                    │
│  Per-session JSONL buffer at                                       │
│  ${XDG_CACHE_HOME:-~/.cache}/kent/transcripts/<session_id>.jsonl,  │
│  appended each turn, then mempalace.sweeper.sweep(path,            │
│  palace_path, source_label="kent"). Idempotent re-sweep is safe.   │
│                                                                    │
│  wake_up() → MemoryStack(palace_path).wake_up(wing="kent")         │
│  recall(q) → Layer3(palace_path).search(q, wing="kent",            │
│                                          n_results=k)              │
└────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
              mempalace ChromaDB at MempalaceConfig().palace_path
              (ChromaDB is the durable store; JSONL is a buffer.)
```

## The `MemoryStore` Protocol (3 methods)

`on_compaction` from earlier drafts is gone. By the time compaction fires,
every head message has already been persisted by `record_turn` in prior
turns. There is nothing extra to capture at the compaction boundary; only
the summary message itself is new, and it derives from already-persisted
content.

`close` is also gone. `record_turn` calls `sweep` synchronously per turn;
nothing is buffered across turns at the store level, so there is nothing
to flush at exit.

```python
# agent/memory/store.py
class MemoryStore(Protocol):
    """Strategy interface. Swap implementations without touching the loop."""

    def record_turn(
        self, messages: list[Message], *, session_id: str
    ) -> None: ...
    """Called after every turn (any terminal reason) with the new messages
       added this turn. Must be cheap and must not raise — backend errors
       are swallowed and logged."""

    def wake_up(self) -> str: ...
    """Return short recap text injected at session start AND embedded into
       compaction summary messages. Empty string if no memory yet."""

    def recall(self, query: str, k: int = 5) -> str: ...
    """Backing call for the memory_recall tool. Returns model-ready text."""
```

Implementations:

- `MemPalaceStore` — real impl. Default-on in `agent.run()`.
- `NullMemoryStore` — lives in `tests/conftest.py` only. Not exported from
  `agent.memory`. The orthogonality boundary is the Protocol; users who want
  to disable memory implement their own no-op or pass a `MemPalaceStore`
  pointed at a tmp palace.

## How `MemPalaceStore` writes

Mempalace has no in-memory ingest API (verified by reading
`mempalace/sweeper.py` on `main`); ingest is JSONL-only. We work with that
constraint:

1. At `MemPalaceStore.__init__`, generate `session_id = uuid4().hex`. Open
   `${XDG_CACHE_HOME:-~/.cache}/kent/transcripts/<session_id>.jsonl` for
   append. This directory is a transient buffer; chromadb is the durable
   store. (Future: prune old files on a schedule. Out of scope.)
2. `record_turn(messages, session_id=…)` writes one JSONL record per
   message in Claude-Code-format (so `parse_claude_jsonl` accepts it):
   - `type`: `"user"` | `"assistant"`
   - `sessionId`: our session_id
   - `uuid`: `uuid4().hex` per message
   - `timestamp`: ISO 8601 UTC
   - `message`: `{"role": ..., "content": ...}` — content is a string for
     plain user messages, or a list of blocks
     `[{"type": "text", "text": ...}, {"type": "tool_use", "name": ...,
     "input": ...}]` for assistant turns with tool calls, and
     `[{"type": "tool_result", "tool_use_id": ..., "content": ...}]` for
     tool result messages.
3. After flushing the append, call
   `mempalace.sweeper.sweep(transcript_path, palace_path, source_label="kent")`.
   Idempotent: the deterministic drawer ID means re-sweeping is a no-op for
   already-ingested records.

**What we do NOT do:** call `convo_miner.mine_convos` (batch import path,
not live streaming) or write directly to the ChromaDB collection (sweeper
handles dedup and the post-1.5.4 hnswlib quirks).

## How `MemPalaceStore` reads

- `wake_up()` → `MemoryStack(palace_path).wake_up(wing="kent")`. Per
  upstream, returns 600–900 tokens of L0 + L1. Used in two places:
  1. Injected as a `{"role": "system", "content":
     "<recalled-memory>{text}</recalled-memory>"}` message at session start.
  2. **Embedded inline into the compaction summary message** (see below).
- `recall(q, k)` → `Layer3(palace_path).search(q, wing="kent", n_results=k)`.
  Returns formatted text. We deliberately use `Layer3.search` instead of
  `searcher.search` because `searcher.search` *prints* to stdout rather
  than returning (verified in `mempalace/searcher.py:218`).

## Compaction folds wake-up into the summary

Re-injecting wake-up as a separate post-compaction message would either
grow the message list unbounded or require sentinel scan-and-replace
bookkeeping. Cleaner: have `maybe_compact` build a single combined system
message:

```python
summary = await _summarize(head, llm)
recalled = memory_store.wake_up() if memory_store else ""
parts = [f"<conversation-summary>{summary}</conversation-summary>"]
if recalled:
    parts.append(f"<recalled-memory>{recalled}</recalled-memory>")
summary_msg = {"role": "system", "content": "\n".join(parts)}
new_messages = (summary_msg, *tail)
```

One system message lifecycle. Each compaction overwrites the previous
combined message. No sentinel scan, no message-list growth, and the model's
recall priming is refreshed each compaction with the latest L1.

## When memory hooks fire

| Loop event                                                             | Hook                              |
|------------------------------------------------------------------------|-----------------------------------|
| `agent/loop.py:74` — top of every turn, just before `maybe_compact`    | (no change)                       |
| `agent/compact.py:40` — building summary message                       | embed `memory_store.wake_up()` inline |
| `agent/loop.py:124` — terminal "completed" path                        | `memory_store.record_turn(turn)`  |
| `agent/loop.py:156` — between turns when more turns will follow        | `memory_store.record_turn(turn)`  |
| `model_error`, `max_turns`, `tool_loop`, `aborted`, `context_overflow` | `memory_store.record_turn(turn)`  |
| Tool call: `memory_recall(query, k)`                                   | `memory_store.recall(query, k)`   |
| REPL start / `kent run` start                                          | `memory_store.wake_up()` injected |

The terminal-path fix (today's loop only fires `on_turn_end` on `completed`
and `next_turn`; all other terminals skip it, losing the in-progress turn
on a crash) **lands as a separate one-line PR first** since it's a
standalone bug independent of mempalace. This plan assumes that PR is in.

## Files to change / add

### New files

| Path                                       | Purpose                                                           |
|--------------------------------------------|-------------------------------------------------------------------|
| `agent/memory/__init__.py`                 | Re-export `MemoryStore`, `MemPalaceStore`                         |
| `agent/memory/store.py`                    | `MemoryStore` Protocol                                            |
| `agent/memory/mempalace_store.py`          | `MemPalaceStore` (lazy-imports mempalace inside `__init__`)       |
| `agent/memory/transcript.py`               | JSONL writer producing Claude-Code-compatible records             |
| `agent/builtin/memory_recall.py`           | `MemoryRecall` tool wrapping `MemoryStore.recall`                 |
| `tests/test_memory_store.py`               | Records JSONL correctly; calls sweep once per turn                |
| `tests/test_memory_transcript.py`          | Round-trip: write JSONL → `parse_claude_jsonl` reads it back      |
| `tests/integration/test_mempalace.py`      | Real mempalace round-trip; gated by `pytest.importorskip`         |
| `tests/integration/test_memory_e2e.py`     | Live-LLM end-to-end across sessions, post-compact recall          |
| `tests/integration/test_memory_scale.py`   | **The real long-term test** — 200 sessions, needle retrieval      |
| `tests/conftest.py` (changed)              | Defines `NullMemoryStore` privately + autouse fixture             |

### Changed files

| Path                          | Change                                                                                                     |
|-------------------------------|------------------------------------------------------------------------------------------------------------|
| `agent/loop.py`               | Add `memory_store: MemoryStore \| None = None` kwarg; lazy-construct `MemPalaceStore()` when None; thread to `maybe_compact`; ensure `record_turn` fires on every terminal path; pass `session_id` to record_turn (auto-generated by the store; loop just passes whatever id it knows about). |
| `agent/compact.py`            | Add optional `memory_store` kwarg; when present, embed `wake_up()` text inline in the summary message. Keep 2-arg signature so `test_compact_signature_drops_system_param` still passes. |
| `agent/cli.py`                | `_repl()` constructs one `MemPalaceStore` per REPL session, passes to `run()`; injects wake-up as a system message at REPL start; registers `MemoryRecall` tool; new slash commands `/memory`, `/recall <q>`, `/forget` (session-scoped delete with confirmation). `cmd_doctor` adds a `[memory]` block (palace_path, drawer count, last-write timestamp). |
| `agent/__init__.py`           | Export `MemoryStore`, `MemPalaceStore`, `MemoryRecall`. (NOT `NullMemoryStore` — test seam only.) |
| `pyproject.toml`              | Add `mempalace>=3.3` to `[project].dependencies` (NOT optional — default-on requirement).                  |
| `README.md`                   | New `## Persistent memory` section: how it works, the verbatim-storage caveat (secrets), `/forget` for cleanup. |

### Functions/utilities to reuse

From **mempalace** (verified by reading source on `main`):

- `mempalace.config.MempalaceConfig().palace_path`
- `mempalace.sweeper.sweep(jsonl_path, palace_path, source_label) -> dict`
- `mempalace.sweeper.parse_claude_jsonl(path) -> Iterator[dict]` (used in
  test round-trip)
- `mempalace.layers.MemoryStack(palace_path).wake_up(wing) -> str`
- `mempalace.layers.Layer3(palace_path).search(query, wing, room, n_results) -> str`

From the **agent** package:

- `LoopState.advance(**changes)` (`agent/state.py:26`) — for any new state
  field if needed (none currently planned).
- `on_turn_end` callback already present in `run()` (`agent/loop.py:56`) —
  chained with `memory_store.record_turn` so user callbacks still work.
- `ToolRegistry.register` (`agent/tools.py:41`) — register `MemoryRecall`.
- `Tool` Protocol with `is_concurrency_safe` (`agent/tools.py:17`) — recall
  is read-only; True so it batches with `web_search`/`web_fetch`.

## Critical implementation details

1. **Avoiding circular imports.** `agent/loop.py` must not import
   `MemPalaceStore` at top level (mempalace pulls chromadb, which is heavy).
   Use a `_default_store()` factory that imports lazily inside `run()` when
   `memory_store is None`.
2. **Errors must not break the loop.** All `MemoryStore` methods called
   from inside the loop are wrapped in `try/except` with `logging.warning`.
   A broken palace must never break a conversation. Mempalace itself is
   already defensive (sweeper logs and returns on failure).
3. **JSONL format conformance.** A round-trip test writes a synthetic
   conversation via our writer, calls
   `mempalace.sweeper.parse_claude_jsonl(path)`, and asserts every record
   came through with `session_id`, `uuid`, `timestamp`, `role`, `content`.
4. **Tool calls in JSONL.** Assistant messages with tool calls encode as
   content blocks `[{"type": "text", "text": ...}, {"type": "tool_use",
   "name": ..., "input": ...}]`; tool results as the next message with role
   `"user"` and content `[{"type": "tool_result", "tool_use_id": ...,
   "content": ...}]` — what `_flatten_content` (sweeper.py) expects.
5. **Empty wake-up.** If `wake_up()` returns empty (no memory yet on first
   run), inject nothing — no empty system message at session start, and the
   compaction summary message contains only the `<conversation-summary>`
   block.
6. **Test compatibility.** `test_compact_signature_drops_system_param`
   (`tests/test_compact.py:85`) calls `maybe_compact(state, llm)` with two
   positional args. Solution: `memory_store` is a kwarg with default `None`.
7. **`session_id` lifecycle.** Generated in `MemPalaceStore.__init__` once
   per store instance. The transcript file is named after it; every JSONL
   record carries it as `sessionId`. The loop passes this id through to
   `record_turn` so the same id is used end-to-end.
8. **Default-on store breaks the existing unit suite.** Once `agent.run()`
   constructs a `MemPalaceStore` by default, every test that calls `run()`
   would touch a real Chroma palace. Mitigation: `tests/conftest.py`
   defines a private `NullMemoryStore` and an autouse fixture that
   monkeypatches `agent.loop._default_store` to return it. The `memory`
   pytest marker opts back into the real store for the integration suite.

## Phased rollout

0. **Phase 0 (separate PR) — terminal-path bug fix.** Move `on_turn_end`
   into a `finally`-style block in `loop.py` so it fires once per turn on
   every terminal reason. Has standalone value; no mempalace coupling.
   Land first.
1. **Phase 1 — Protocol + transcript writer.** Land `MemoryStore`,
   `agent/memory/transcript.py`, and the conftest `NullMemoryStore`
   fixture. Wire `memory_store` kwarg through `run()` and `maybe_compact`.
   Default the loop's store to the conftest null in tests, leave production
   default at `None` for now (so existing tests stay green). New tests
   exercise the wiring with a `RecordingMemoryStore` double.
2. **Phase 2 — `MemPalaceStore` + lib default.** Implement `MemPalaceStore`.
   Flip `agent.run()`'s `_default_store` factory to construct
   `MemPalaceStore()`. Add `mempalace>=3.3` to `pyproject.toml`. Confirm
   the autouse fixture keeps the unit suite offline.
3. **Phase 3 — Compaction integration.** Embed `wake_up()` text inline in
   the compaction summary message. Add the round-trip + post-compaction
   recall tests.
4. **Phase 4 — Tool + CLI integration.** Register `MemoryRecall` in the
   built-in registry. CLI `_repl()` injects wake-up at REPL start, propagates
   the same `MemPalaceStore` instance across turns, adds `/memory`,
   `/recall`, `/forget` slash commands and the `cmd_doctor` block.

## Verification

### Unit (offline, default suite)

- `pytest tests/test_memory_store.py` — Construct `MemPalaceStore` with a
  fake transcript dir + monkeypatched `sweep`/`MemoryStack`/`Layer3`;
  assert `record_turn` writes one JSONL line per message and calls `sweep`
  exactly once per turn.
- `pytest tests/test_memory_transcript.py` — write a 4-turn conversation
  (user → assistant w/ tool_use → tool_result → assistant), then read it
  back via `mempalace.sweeper.parse_claude_jsonl` (real import) and assert
  every `(session_id, uuid, role, content)` matches.
- `pytest tests/test_compact.py` — existing tests must still pass
  (regression on the 2-arg signature). Add a new test asserting the summary
  message contains both `<conversation-summary>` and `<recalled-memory>`
  when a memory store is supplied, and only `<conversation-summary>` when
  it isn't.
- `pytest tests/test_loop.py` — add cases proving `record_turn` fires on
  `model_error` / `max_turns` / `tool_loop` / `aborted` /
  `context_overflow` terminals (using `RecordingMemoryStore`). These rely
  on Phase 0 having landed.

### Integration — store-only (opt-in, marker `memory` + import skip)

- `pytest tests/integration/test_mempalace.py -m memory` —
  - Construct a real `MemPalaceStore(palace_path=tmp_path)`.
  - Run two synthetic sessions: session A's `record_turn` writes "favorite
    color is octarine"; construct a *new* `MemPalaceStore` against the
    same `palace_path`; session B's `wake_up()` text contains "octarine"
    AND `recall("favorite color")` returns the matching drawer. (Both
    pathways asserted, not "either" — see e2e test 1 below for the
    weaker LLM-recall variant.)

### Integration — live LLM end-to-end (opt-in, markers `memory` + `integration`)

These answer "does the model actually remember things across sessions once
the integration is wired up?" They follow the existing
`tests/integration/test_ollama.py` pattern: `pytestmark = [integration,
memory]`, gated by `pytest.importorskip("mempalace")` plus a check for
`OLLAMA_HOST`/`ATLASCLOUD_API_KEY`. Each test constructs a real
`OpenAICompatibleLLM` (cheap local model) and a real
`MemPalaceStore(palace_path=tmp_path)` so the palace is isolated per test.

`tests/integration/test_memory_e2e.py` covers four scenarios:

1. **Cross-session recall (model-driven).** Session A: ask the model to
   remember "my favorite color is octarine," then drop the store.
   Construct a new `MemPalaceStore` pointed at the same `palace_path`
   (sweeper IDs are deterministic, no fork needed). Session B: ask
   "what's my favorite color?" Assert `"octarine"` appears in either the
   injected wake-up text or a `memory_recall` tool result. The
   "either-or" weakness here is intentional and documented: the
   stronger guarantee on the underlying recall is asserted by
   `test_mempalace.py` above and by the scale test below; this test only
   asks "does the LLM use what we surface."
2. **Pre-seeded memory (LLM-isolation).** Skip the conversation entirely:
   call `store.record_turn(...)` directly with synthetic turns containing
   ~50 fictional facts. Then run a real session asking about one of
   them. Isolates "is the integration plumbing correct" from "does the
   LLM use what we surface."
3. **Survives forced compaction.** Don't cripple `llm.context_window` —
   doing so also cripples `_summarize`, which is a recursive LLM call on
   the same model. Instead `monkeypatch.setattr(agent.compact,
   "COMPACT_THRESHOLD", 0.3)` and `COMPACT_KEEP_TAIL = 2`. State the fact
   in turn 1, fill turns 2–5 with filler, then ask about it in turn 6.
   Wrap the store in a `RecordingMemoryStore` that delegates to
   `MemPalaceStore` while logging calls; assert (a) the post-compaction
   summary message contains `<recalled-memory>` and a substring of the
   fact, and (b) the model's final answer surfaces it.
4. **Fault injection.** A `BrokenMemoryStore` whose `record_turn` raises
   on every call; run one real LLM turn; assert
   `Terminal(reason="completed")`. Makes Critical Detail #2 load-bearing.

### Integration — scale (the real long-term test)

`tests/integration/test_memory_scale.py` is what actually validates the
core problem this plan exists to solve. Markers: `memory + integration +
slow`. Triple opt-in.

**Test setup (deterministic, no LLM during seed):**

- Construct one `MemPalaceStore(palace_path=tmp_path)`.
- Pre-seed 200 synthetic sessions, each 5–10 turns of plausible-looking
  conversation across mixed topics (cooking, travel, code, etc.). Use a
  fixed RNG seed for reproducibility.
- Plant a single needle fact in session 47, turn 3:
  `"my emergency contact code is QUARTZ-7741"`.
- After seeding: ~1000–2000 total drawers in the palace.
- Drop the seed store, construct a fresh `MemPalaceStore` against the
  same palace.

**Test scenarios (real LLM):**

A. **Direct recall path.** Call `store.recall("emergency contact code", k=5)`
   directly. Assert the needle drawer is in the top 5 results. This is
   pure mempalace + ChromaDB; no LLM involved. Establishes a recall floor.

B. **End-to-end with model.** Run a real LLM turn asking "what's my
   emergency contact code?" Assert `"QUARTZ-7741"` appears in the model's
   final answer. Requires (i) the needle in `wake_up()` or in a
   `memory_recall` tool result, AND (ii) the model surfacing it.

C. **Discrimination.** Pre-seed 5 distractor sessions containing similar
   phrasing ("emergency phone number," "contact info," "backup code") but
   different values. Re-run scenario B. Assert the model returns the
   needle value, not a distractor — this catches "ChromaDB returns
   anything topical" false-positives.

**Pass criteria (tighter than 2/3):**

- Scenarios A and C run 5×, require **5/5** passes (deterministic — no LLM
  in A; deterministic up to ChromaDB ranking in C's recall step). Failures
  here are real regressions in mempalace or in our integration.
- Scenario B runs 5×, requires **4/5** passes (one LLM-flake budget).
  Below 4/5 fails the test and surfaces degradation rather than hiding it.

Stability rails for scenarios involving the LLM (B, e2e tests):

- Low temperature (`0.0` if endpoint allows, else `0.1`).
- Substring assertions, not exact match.
- `max_turns=5` to bound runaway tool loops.

Cost control: `@pytest.mark.memory @pytest.mark.integration @pytest.mark.slow`
— triple opt-in. Default CI runs `pytest -m "not integration and not slow"`.
The scale test runs nightly or pre-merge for changes touching
`agent/memory/` or `agent/compact.py`.

### Manual smoke

- `kent` REPL, tell it "remember that my favorite color is octarine," `/exit`.
  Re-launch `kent`. Ask "what's my favorite color?" — should recall via
  wake-up or `memory_recall`.
- Long synthetic conversation that triggers compaction; afterward, ask about
  a fact only stated in the head pre-compaction; expect the model to call
  `memory_recall` and get it back, OR for the post-compaction summary
  message to contain it inline (via `<recalled-memory>`).
- `kent doctor` shows the `[memory]` block with non-zero drawer count after
  the smoke runs above.

## Out of scope for this plan

- MCP-server pathway (mempalace ships 29 MCP tools). kent isn't an MCP host.
- Per-subagent wings (the `Spawn` subagent shares the parent's store).
  **Caveat:** concurrent `record_turn` calls from sibling subagents against
  one JSONL append + one chromadb upsert path are unverified. Track as a
  known limitation in the README; tackle when Spawn-with-memory becomes
  load-bearing.
- Per-project / per-cwd wing scoping (locked to single global `wing="kent"`).
- A separate `kent memory` top-level subcommand (slash commands + `doctor`
  cover REPL needs).
- Encryption-at-rest / redaction of secrets in stored drawers. Document in
  README that the palace stores verbatim content; `/forget` is the
  emergency cleanup.
- Pruning the transcript JSONL buffer dir — it grows unbounded. Add a
  cleanup pass in a follow-up.

## Risks & mitigations

| Risk                                                                      | Mitigation                                                                                                                       |
|---------------------------------------------------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| chromadb install adds ~300 MB and slows first import                      | Lazy-import mempalace inside `MemPalaceStore.__init__`. Tests inject `NullMemoryStore` via the conftest autouse fixture. Document in README under prerequisites. |
| Two `kent` processes racing on the same palace                            | Mempalace claims chromadb 1.5.4+ tolerates this; if we observe corruption, wrap the per-turn `sweep` in `fcntl.flock` on the palace dir. |
| Wake-up text too large after months of usage                              | Mempalace's L1 is bounded (~600–900 token total). If still too noisy, downgrade to L0-only or a lighter custom recap. Track via `cmd_doctor`. |
| Storing secrets verbatim (API keys echoed in tool output)                 | Document loudly in README; `/forget` slash command exists for emergency cleanup. Add a redaction hook in a follow-up phase.    |
| `searcher.search` prints rather than returns                              | We use `Layer3.search` instead — verified to return text (`mempalace/layers.py`).                                              |
| Mempalace upstream maturity (3 weeks old, acknowledged claims-reality gap)| The `MemoryStore` Protocol is the swap point. Replacing mempalace with another backend is one new file implementing the same 3 methods. |
| Concurrent subagent writes from `Spawn`                                   | Documented as a known limitation; defer until Spawn-with-memory is load-bearing.                                                 |
| Disk I/O + chromadb upsert per turn could slow streaming                  | Phase 2 includes a benchmark: time `record_turn` over 100 representative turns; budget is <50ms p95 per turn. If exceeded, move sweep to a background task per turn. |

## Post-implementation deviations

What we built differs from the plan above in two places. Both deviations are
behavior-preserving with respect to the plan's *intent* (isolation, working
recall) but diverge from the *mechanism* the plan named.

### 1. No `wing="kent"` filter — kent gets its own palace instead

The plan repeatedly calls for `wing="kent"` as the isolation filter on
`MemoryStack.wake_up()` and `Layer3.search()`. We implemented this initially,
then discovered via integration tests that `recall()` always returned
"No results found." Reading mempalace source revealed the cause:

`mempalace.sweeper.sweep()` does not write a `wing` field to drawer metadata.
Only `mempalace.miner` (the `mempalace mine <dir>` CLI), `diary_ingest`,
`closet_llm`, and `room_detector_local` tag drawers with wings. The sweeper —
the only ingest path supported by the docs for live JSONL streams — stores
`session_id`, `timestamp`, `message_uuid`, `role`, `source_file`, `filed_at`,
and `ingest_mode`, but never `wing`. So filtering by `wing="kent"` matches
nothing.

The fix preserves the plan's isolation goal but swaps the mechanism: kent
owns its own ChromaDB palace at `~/.kent/palace` (configurable via
`$KENT_HOME`) instead of `~/.mempalace/palace`. With a kent-private palace,
filtering by wing is unnecessary because the palace itself is single-tenant.

Concrete code differences from the plan:

- `MemPalaceStore.__init__` no longer imports `MempalaceConfig`. It defaults
  `palace_path` to `_DEFAULT_PALACE = $KENT_HOME/palace` (a module constant).
- `MemPalaceStore.wake_up()` calls `MemoryStack(...).wake_up()` with no
  args. The wing kwarg is dropped.
- `MemPalaceStore.recall()` calls `Layer3(...).search(query, n_results=k)`.
  The wing kwarg is dropped.
- `kent doctor`'s `[memory]` block imports `_DEFAULT_PALACE` directly so the
  doctor and the store agree on the palace location by construction.

### 2. Compaction integration test patches `_context_window`

The plan says: *"Don't cripple `llm.context_window` — doing so also cripples
`_summarize`, which is a recursive LLM call on the same model."*

We did exactly that in `tests/integration/test_memory_e2e.py::test_survives_forced_compaction`,
patching `_context_window=512`. Reading `agent/llm.py` showed the plan's
reasoning was incorrect: `context_window` is read **only** by
`maybe_compact`'s threshold ratio. `LLM.stream` (which `_summarize` calls)
does not consult it. So shrinking it triggers the threshold without
affecting the summarization call against the real LLM endpoint.

The plan's alternative (`COMPACT_THRESHOLD=0.3` + longer filler) does not
work either: even at 0.3 of a 32k-token AtlasCloud window, six short filler
messages are far below the trigger. Patching `_context_window` is the
simplest test-only path; could be replaced with longer filler messages if
strict plan adherence is preferred.
