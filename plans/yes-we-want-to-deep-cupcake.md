# Plan: Agent diary + wings for kent (no MCP)

## Context

Today kent uses mempalace as a Python library through a deliberately small surface
(`sweeper.sweep`, `MemoryStack.wake_up()`, `Layer3.search`, `MemoryStack.status`).
Two large mempalace capabilities are still untouched:

- **Agent diary** — `mempalace.diary_ingest.ingest_diaries(...)` — file-based
  journal of reflections that gets ingested as one drawer per `(wing, day)` with
  proper wing/room metadata.
- **Wings** — project/intent-scoped partitioning. Reads via `MemoryStack.wake_up(wing)`,
  `MemoryStack.recall(wing, room)`, and `Layer3.search(query, wing, room)` apply a
  ChromaDB `where={"wing": ...}` filter.

We previously deferred both because:
1. The agent diary lives behind mempalace's MCP tool surface and kent isn't an MCP host.
2. Sweeper-ingested turn drawers don't carry a wing field, so wing-filtered reads
   excluded everything — defeating the purpose.

Both objections dissolve when we use the **non-MCP Python entry points** directly
(`mempalace.diary_ingest.ingest_diaries` *is* a public Python function; wings
are usable on the diary path because diary drawers DO carry wing metadata).

The user wants kent to "utilize mempalace as best we can." The intended outcome:
kent gains a wing-per-project model where each thing the user wants kent to do or
monitor becomes a named wing, and a per-wing agent diary captures observations,
findings, decisions, and recurring patterns. Recall stays globally aware (turns
remain reachable cross-project) while wing-scoped recall lets the model lean
into the active project's diary on demand.

## User-locked decisions

1. **Wings = named projects/intents.** Auto-detected from user statements.
   When kent doesn't recognize an intent, it asks the user for a name and intent
   description before creating a new wing.
2. **Diary = lightweight memory stream for one named agent: observations,
   findings, decisions, and recurring patterns.** Per-agent, scoped to the active wing.
3. **Wake-up = global + wing-scoped, concatenated** — at session start.
   (Compaction-time wake-up stays global-only to avoid doubling tokens on a hot
   path; the wing-scoped diary content is recoverable mid-session via
   `memory_recall_here`.)

## Design at a glance

```
kent CLI ──┬─→ active wing resolved from: --wing > $KENT_WING > ${KENT_HOME}/active_wing.txt > "kent_default"
           │
           ├─→ MemPalaceStore.active_wing (state on the store)
           │
           ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ session start: history += <recalled-memory>{wake_up()}</recalled-memory>  │
   │   wake_up() = global L0+L1                                          │
   │             + (if non-empty) wing-scoped L2 daily-room recall       │
   └─────────────────────────────────────────────────────────────────────┘
           │
           ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ during a turn:                                                      │
   │   - record_turn(...)         (sweeper, no wing — unchanged)         │
   │   - tools available:                                                │
   │       memory_recall(query, k)         global L3                     │
   │       memory_recall_here(query, k)    wing-scoped L3                │
   │       diary_write(kind, text, topic?) appends + ingest_diaries      │
   │       set_wing(name, intent?)         switch / register             │
   └─────────────────────────────────────────────────────────────────────┘
           │
           ▼
   ┌─────────────────────────────────────────────────────────────────────┐
   │ compaction: summary message embeds GLOBAL wake_up() only            │
   │   (saves tokens on the hot path; diary still reachable via tool)    │
   └─────────────────────────────────────────────────────────────────────┘
```

### File layout under `${KENT_HOME}` (default `~/.kent/`)

```
${KENT_HOME}/
├── palace/                              # ChromaDB — already exists
├── active_wing.txt                      # one line, current wing name
└── diaries/
    ├── kent_default/
    │   ├── .intent.txt                  # one-line wing description
    │   ├── 2026-04-27.md
    │   └── 2026-04-28.md
    └── prod-deploys/
        ├── .intent.txt
        └── 2026-04-27.md
```

The directory layout *is* the wing registry. No separate `wings.json` — listing
wings = `iter ${KENT_HOME}/diaries/*/`.

### Diary file format

Markdown with `## ` headers (matches `diary_ingest.DIARY_ENTRY_RE`):

```markdown
# 2026-04-27

## 14:32:01 [agent=kent] [OBSERVATION] build slowdown
The build pipeline got 30% slower after midnight.

## 15:08:44 [agent=kent] [DECISION] use feature flag
Decided to gate the new ranker behind FF_RANKER_V2.
```

Kind (`OBSERVATION` / `FINDING` / `DECISION` / `PATTERN`) lives in the header
text — searchable as substring via Layer3 BM25. Per-kind metadata filtering is
deferred to v2 (would require vendoring `ingest_diaries`).

## Architectural decisions (with reasoning)

- **Wing-scoped wake-up uses `MemoryStack.recall(wing, room="daily")`, NOT `wake_up(wing)`.**
  L1 ranks by `importance` and `ingest_diaries` doesn't set importance, so diaries
  get crowded out by older "important" turns. L2 by-room retrieval surfaces
  diary entries cleanly. (Verified: `mempalace/layers.py:126-141` for L1's
  importance-sort; `layers.py:185-235` for L2's metadata-filter path.)

- **Wing-scoped pass detects empty-result sentinel** to skip ChromaDB's literal
  "No drawers found." text. Keeps the wake-up message clean for fresh wings.

- **`WingedMemoryStore(MemoryStore)` extension Protocol** preserves back-compat.
  The base `MemoryStore` Protocol (`agent/memory/store.py:9-23`) keeps its 3
  methods unchanged; new methods (`active_wing`, `set_active_wing`,
  `recall_in_wing`, `write_diary`) live in a new optional `WingedMemoryStore`
  Protocol. Tools that need wing capability `isinstance`-check before exposing
  wing args. External users implementing the base Protocol stay green.

- **Sibling tools `MemoryRecall` + `MemoryRecallHere`** rather than overloading
  `MemoryRecall` with a wing arg. Mirrors the `web_search` / `web_fetch` pattern
  (`agent/builtin/web_search.py`, `agent/builtin/web_fetch.py`) — clearer model
  affordance, no Protocol break.

- **Single-call `set_wing(name, intent=None)`** instead of a two-call handshake.
  No precedent for human-in-the-loop tool confirmation in the codebase
  (`Spawn` doesn't confirm; `/forget`'s `input()` is a slash, not a tool). When
  the wing doesn't exist and `intent` is missing, the tool returns an error
  string telling the model: "Wing X doesn't exist. Confirm with the user, then
  re-call with intent='...'.". Conversational responsibility lives with the
  model.

- **Compaction wake-up stays global-only.** `agent/compact.py:48-51` calls
  `memory_store.wake_up()` for the compaction summary. We keep it as a single
  global call. Wing-scoped block runs at session start only. Saves ~50% of
  wake-up tokens on every compaction with no behavioral loss (diaries still
  recoverable via `memory_recall_here`).

- **Active wing is store state, persisted to disk.** `${KENT_HOME}/active_wing.txt`.
  CLI `--wing`, `$KENT_WING`, and `/wing` slash override; `set_wing` tool
  mutates and persists. Subagents inherit parent's store (see `Spawn`
  `agent/builtin/spawn.py:37-45`) — v1 single-agent makes wing mutation by
  subagent benign; document the inheritance.

- **`ingest_diaries` state file at `~/.mempalace/state/`** is unfixable from
  kent's side (path is hard-coded in `diary_ingest.py:39-49`). Acceptable —
  state file is sha-keyed by `(palace_path, diary_dir)` so no cross-tool
  collision is possible. Document in README.

- **Wing name sanitization:** `^[a-z0-9][a-z0-9_-]{0,63}$`. Lowercase
  alphanumerics + `_` + `-`, 1–64 chars, must not start with `_`/`-`/digit.
  Rejected names surface a clear error. Lowercase guard prevents
  case-insensitive filesystem collisions on macOS APFS.

## Phased rollout (5 PRs)

### PR1 — Wing primitives + active-wing persistence

Foundation only. No diary, no tools, no slash commands yet.

**New files:**
- `agent/memory/wings.py` — pure helpers, no mempalace import:
  - `sanitize_wing(name) -> str` (raises `ValueError` on invalid)
  - `kent_home() -> Path`
  - `diaries_root() -> Path` = `kent_home() / "diaries"`
  - `wing_dir(name) -> Path`
  - `list_wings() -> list[str]` — directory enumeration
  - `read_active_wing(default="kent_default") -> str` — reads `active_wing.txt`
  - `write_active_wing(name) -> None`
  - `read_intent(name) -> str | None`, `write_intent(name, text) -> None`
- `tests/test_memory_wings.py` — sanitize edge cases (12+), persistence
  round-trip, dir listing skips non-wing dirs, intent file round-trip.

**Modified files:**
- `agent/memory/store.py` — add `WingedMemoryStore(MemoryStore, Protocol)` with
  `active_wing` property, `set_active_wing(name)`, `recall_in_wing(query, k)`,
  `write_diary(kind, text, *, topic=None)`. Existing `MemoryStore` Protocol
  unchanged. Export both from `agent/memory/__init__.py`.
- `agent/memory/mempalace_store.py` — add `_active_wing` field. Constructor
  resolves from `read_active_wing()`. Add `active_wing` property,
  `set_active_wing(name)` (validates + persists). Stub `recall_in_wing` and
  `write_diary` to raise `NotImplementedError` (filled in PR2/PR4).
- `tests/conftest.py` — extend `NullMemoryStore`: `active_wing="test"`,
  `set_active_wing` no-op, `recall_in_wing` returning `""`, `write_diary` no-op.

### PR2 — Diary writer + `diary_write` tool + `/diary` slash

**New files:**
- `agent/memory/diary.py`:
  - `DiaryWriter(palace_path, kent_home)`
  - `write(wing, kind, text, topic=None)` — atomic append under `fcntl.flock`,
    then call `mempalace.diary_ingest.ingest_diaries(diary_dir=<diaries>/<wing>,
    palace_path=<palace>, wing=<wing>, force=False)`. Lazy mempalace import +
    error swallowing (mirror `MemPalaceStore.record_turn` at
    `agent/memory/mempalace_store.py:43-51`).
  - File format: `## HH:MM:SS [agent=kent] [<KIND>]<topic suffix>\n<body>\n\n`.
  - One file per `<wing>/YYYY-MM-DD.md`.
- `agent/builtin/diary_write.py` — `DiaryWrite(store)` tool. `Args(kind:
  Literal["OBSERVATION","FINDING","DECISION","PATTERN"], text: str, topic:
  str | None = None)`. `is_concurrency_safe = False`. Delegates to
  `store.write_diary(...)`. Returns terse confirmation.
- `tests/test_memory_diary.py` — mempalace mocked: `ingest_diaries` called once
  with correct kwargs after each write; markdown layout matches; `fcntl.flock`
  acquired/released; concurrent appends serialize.

**Modified files:**
- `agent/memory/mempalace_store.py` — implement `write_diary` via `DiaryWriter`.
- `agent/memory/__init__.py` — export `DiaryKind`, `DiaryWriter`.
- `agent/builtin/__init__.py` — export `DiaryWrite`.
- `agent/__init__.py` — export `DiaryWrite`.
- `agent/cli.py` — register `DiaryWrite(memory_store)` in `_repl()`
  (line ~483, alongside `MemoryRecall`) and `cmd_run()` (line ~578). Add
  `/diary <text>` to `_handle_slash` (insert near line ~436); slash defaults
  `kind="OBSERVATION"`. Update `SLASH_HELP` (line ~371).

### PR3 — Wing tool + system-prompt augmentation + CLI flags

**New files:**
- `agent/builtin/set_wing.py` — `SetWing(store)` tool. `Args(name: str, intent:
  str | None = None)`.
  - If wing exists: switch (call `store.set_active_wing(name)`).
  - If wing missing AND `intent` provided: register
    (`write_intent(name, intent)`, mkdir, switch).
  - If wing missing AND `intent` missing: return error string instructing
    model to confirm with user and provide intent.
  - `is_concurrency_safe = False`.
- `tests/test_set_wing_tool.py` — switch existing, register-then-switch,
  error-on-missing-no-intent, sanitization rejection.

**Modified files:**
- `agent/cli.py`:
  - Add `--wing <name>` to `kent` and `kent run` argparse parsers
    (around lines 716-754 — find existing flag definitions).
  - Wing resolution helper: `_resolve_wing(args) -> str` checks
    `args.wing > os.environ["KENT_WING"] > read_active_wing() > "kent_default"`.
  - `_repl()` and `cmd_run()` resolve wing → `memory_store.set_active_wing(...)`.
  - Add `/wing` (show current), `/wing <name>` (switch), `/wings` (list with
    intents) to `_handle_slash`.
  - Add `SetWing(memory_store)` registration alongside other tool registers.
  - Build system prompt dynamically: append a list of current wings (capped at
    20 most recent, with intent text) and a brief instruction: "When the user
    states a project intent that doesn't match an existing wing, propose
    `set_wing(name=...)` after confirming the name with the user, then call
    again with `intent=...` to register." Find the existing `SYSTEM_PROMPT`
    constant (line ~65) and wrap into a builder function.
  - Update `SLASH_HELP`.

### PR4 — Wing-scoped wake-up + `memory_recall_here`

**New files:**
- `agent/builtin/memory_recall_here.py` — `MemoryRecallHere(store)`. `Args(query:
  str, k: int = 5)`. Uses `store.active_wing` implicitly; model can't override.
  `is_concurrency_safe = True`. Delegates to `store.recall_in_wing(query, k)`.
- `tests/integration/test_diary.py` — marker `memory`:
  - `test_diary_write_then_recall_in_wing` — round-trip via real palace.
  - `test_cross_wing_isolation` — wing A entry not visible in wing B recall.
  - `test_wing_scoped_wake_up_format` — wake-up contains both global and
    wing-scoped blocks when both have content.
  - `test_empty_wing_falls_back_to_global` — fresh wing → no
    "No drawers found" leaks into wake-up.
  - `test_diary_force_reingest_purges_old_closets` — verify mempalace
    idempotency contract holds.

**Modified files:**
- `agent/memory/mempalace_store.py`:
  - `wake_up()` builds: `MemoryStack(palace).wake_up()` →
    `MemoryStack(palace).recall(wing=active_wing, room="daily", n_results=10)`.
    If second call returns empty / starts with `"No drawers found"` /
    `"No palace"`, omit. Concat with `\n\n` separator. Keep error swallowing
    (current pattern at lines 53-60).
  - `recall_in_wing(query, k=5)`:
    `Layer3(palace).search(query, wing=active_wing, room="daily", n_results=k)`.
    Same try/except wrap.
- `agent/builtin/__init__.py` + `agent/__init__.py` — export
  `MemoryRecallHere`.
- `agent/cli.py` — register `MemoryRecallHere`; add `/recall-here <q>` slash.
- **Important non-change:** `agent/compact.py:28-58` is unchanged.
  `maybe_compact` still calls `memory_store.wake_up()` — but `MemPalaceStore.wake_up()`
  now returns the dual block. To keep compaction tokens bounded, factor
  `wake_up()` into two methods on `MemPalaceStore`:
  - `wake_up()` → global only (used by `compact.py`).
  - `wake_up_full()` → global + wing-scoped concat (used at session start in
    CLI).
  Add `wake_up_full` to `WingedMemoryStore` Protocol.

### PR5 — Doctor blocks, README, e2e tests

**Modified files:**
- `agent/cli.py` `cmd_doctor` (lines ~644-711):
  - Add `[wings]` block: list wings (each line: name, active marker, intent if
    set, file count). Cap at 50; truncate with "(N more...)".
  - Add `[diary]` block: per-wing summary — last-write timestamp + total
    entries (count of `## ` lines across all `*.md` in the dir).
- `tests/integration/test_memory_e2e.py`:
  - `test_model_driven_diary_write_and_recall` — system A: user states a
    project + asks kent to remember a fact; assert `diary_write` was invoked +
    file content matches. System B: same wing, ask "what did you note about
    X?" — model surfaces fact via `memory_recall_here` or wake-up.
  - `test_set_wing_handshake_via_model` — user states a new project;
    assert at least one `set_wing` call has both `name` and `intent` after the
    model exchanges with the user.
- `README.md` — replace the "Why no `wing` filter?" note. Add new "Wings &
  diary" section explaining: filesystem layout, wing creation flow,
  `diary_write` + `memory_recall_here`, the verbatim-storage caveat, the
  `~/.mempalace/state/` leak, the no-per-entry-delete limitation.

## Critical files to modify

| Path | Why |
|---|---|
| `/Users/kennethchambers/Documents/GitHub/kent/agent/memory/store.py` | Add `WingedMemoryStore` extension Protocol |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/memory/mempalace_store.py` | Active wing state; dual wake-up; `recall_in_wing`; `write_diary` |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/memory/wings.py` (new) | Wing path/registry helpers |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/memory/diary.py` (new) | `DiaryWriter` |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/builtin/diary_write.py` (new) | `DiaryWrite` tool |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/builtin/set_wing.py` (new) | `SetWing` tool |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/builtin/memory_recall_here.py` (new) | Wing-scoped recall tool |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/cli.py` | Flags, slashes, system-prompt builder, doctor blocks, tool registration |
| `/Users/kennethchambers/Documents/GitHub/kent/tests/conftest.py` | Extend `NullMemoryStore` |
| `/Users/kennethchambers/Documents/GitHub/kent/README.md` | "Wings & diary" section |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/__init__.py` | Re-export new tools |
| `/Users/kennethchambers/Documents/GitHub/kent/agent/builtin/__init__.py` | Re-export new tools |

**Not modified (intentionally):**
- `agent/loop.py` — already threads `memory_store` everywhere; no new seam needed.
- `agent/compact.py` — keep using `wake_up()` (global-only after PR4 split).
- `pyproject.toml` — `mempalace>=3.3` already covers `diary_ingest`.

## Existing functions/utilities to reuse

From **mempalace** (Python — no MCP):
- `mempalace.diary_ingest.ingest_diaries(diary_dir, palace_path, wing, force=False)`
  — `diary_ingest.py:75` (verified signature, idempotent).
- `mempalace.layers.MemoryStack(palace).wake_up()` — already used at
  `mempalace_store.py:57`.
- `mempalace.layers.MemoryStack(palace).recall(wing, room, n_results)` —
  `layers.py:398`. New use for wing-scoped wake-up block.
- `mempalace.layers.Layer3(palace).search(query, wing, room, n_results)` —
  already used at `mempalace_store.py:66`. Pass `wing` and `room="daily"` for
  diary recall.
- `mempalace.layers.MemoryStack(palace).status()` — already used in doctor.

From **kent**:
- `MemPalaceStore`'s lazy-import + try/except pattern (`agent/memory/mempalace_store.py:43-69`)
  — replicate in every new method touching mempalace.
- `_DEFAULT_PALACE` module constant pattern (`mempalace_store.py:26-28`) —
  define `_DIARIES_ROOT` next to it.
- `_handle_slash` elif-chain (`agent/cli.py:384-468`) — extension point for new
  slashes.
- `cmd_doctor` `[memory]` block (`agent/cli.py` ~660-700) — template for
  `[wings]` and `[diary]` blocks.
- `Spawn.__init__(parent_registry, llm, memory_store)` (`agent/builtin/spawn.py:37-45`)
  — pattern for tools needing the store.
- `MemoryRecall(store)` (`agent/builtin/memory_recall.py`) — copy structure for
  `MemoryRecallHere` and `DiaryWrite`.
- `tests/conftest.py` autouse `NullMemoryStore` fixture — extend, don't fork.
- `tests/integration/test_mempalace.py::test_record_and_recall_cross_session`
  — template for `tests/integration/test_diary.py` cross-session pattern.

## Risks

| Risk | Mitigation |
|---|---|
| ChromaDB equality filter excludes wing-less drawers — turn drawers absent from wing-scoped wake-up | Frame wing-scoped pass as **diary-only** ("agent's notebook for this wing"). Document in README. |
| L1 wake-up ranks by importance — diaries get crowded out by older important turns | Use **Layer 2** (`MemoryStack.recall(wing, room="daily")`) for the wing-scoped pass instead of `wake_up(wing)`. |
| Wing-scoped pass returns "No drawers found" sentinel — pollutes wake-up | Sentinel check in `MemPalaceStore.wake_up_full()`: skip block when result starts with `"No drawers found"` / `"No palace"` / is empty after strip. |
| Compaction token cost doubles with dual wake-up | Compaction calls `wake_up()` (global only); session start calls `wake_up_full()` (dual). |
| Existing external `MemoryStore` implementers break if Protocol grows | Add `WingedMemoryStore` as separate optional Protocol; tools `isinstance`-check before exposing wing args. |
| `~/.mempalace/state/` write outside `$KENT_HOME` | Hard-coded in mempalace; sha-keyed per (palace, dir) so no collision. Document. |
| Wing name collisions on case-insensitive filesystems (macOS APFS) | Sanitization forces lowercase. |
| Concurrent diary appends from two kent instances | `fcntl.flock` on the file before append. `mempalace.diary_ingest` also takes `mine_lock` per source — belt-and-suspenders. |
| Diary file appended but `ingest_diaries` raises before completing | State file + deterministic drawer ID = next successful ingest reconciles. Document; no transactions. |
| Wing rename/delete not supported | Document as v1 limitation. Renaming a wing orphans drawers (different `(wing, date)` hash). v2 work. |
| Per-entry diary delete not supported | `/forget` only clears session transcript. Diary edits require manual `.md` edit + `force=True` reingest. Add `/diary-edit <date>` (opens `$EDITOR`) in v2. |
| Subagents inherit parent's `active_wing` and may mutate via `set_wing` | Document. v1 single-agent ("kent") makes this benign. |
| Wing list in system prompt grows unbounded | Cap at 20 most-recently-used wings; truncate with "(N more — use /wings)". |
| `set_wing` called mid-turn — model uses new wing for THIS turn's `memory_recall_here` | Acceptable; deliberate. Document. |

## Verification

### Unit (offline, default suite — `pytest -m "not integration and not slow"`)

- `tests/test_memory_wings.py` — sanitize cases (lowercase, alnum, length,
  reserved prefixes, unicode), `read/write_active_wing` round-trip, intent
  file round-trip, `list_wings` skips non-dirs and dot-dirs.
- `tests/test_memory_diary.py` — markdown layout matches spec; mempalace
  mocked, `ingest_diaries` called once per write with correct kwargs;
  `fcntl.flock` acquired then released; two writes append (don't clobber);
  swallow-on-mempalace-error.
- `tests/test_set_wing_tool.py` — switch existing wing; missing+no-intent →
  error string mentions "intent"; missing+with-intent → registers + switches;
  invalid name → error.
- `tests/test_memory_store.py` (extend) — `active_wing` reads/writes
  `active_wing.txt`; `WingedMemoryStore` `isinstance` check works; existing
  tests stay green.
- `tests/test_compact.py` (regression) — assert summary message contains
  global wake-up only (no `<wing-recalled>` block); existing
  `test_compact_signature_drops_system_param` still passes.

### Integration with marker `memory` (real mempalace, no LLM)

- `tests/integration/test_diary.py`:
  - `test_diary_write_then_recall_in_wing`
  - `test_cross_wing_isolation`
  - `test_wing_scoped_wake_up_format` (both blocks present)
  - `test_empty_wing_falls_back_to_global` (no sentinel leak)
  - `test_diary_idempotent_reingest`
  - `test_diary_force_reingest_purges_old_closets`

### Integration with markers `memory + integration` (live LLM)

- `tests/integration/test_memory_e2e.py` (extend):
  - `test_model_driven_diary_write_and_recall`
  - `test_set_wing_handshake_via_model`
  - `test_diary_survives_compaction` — fact in diary, compaction triggered,
    model can still answer via `memory_recall_here`.

### Manual smoke

1. `kent --wing test1` → `/diary I noticed the build slows after midnight` →
   `/exit` → `kent --wing test1` → "what have you noticed about builds?" →
   model surfaces via wake-up wing block or `memory_recall_here`.
2. `kent` → "I want kent to monitor my terraform deploys" → model proposes
   `set_wing(name="...")`, asks user, then re-calls with intent.
   `${KENT_HOME}/diaries/<name>/.intent.txt` contains the intent.
3. `kent doctor` after #1 and #2 → `[wings]` lists both; `[diary]` shows
   counts.
4. Long conversation triggering compaction with diary entries pre-existing →
   compaction summary stays single-block (no wing block); `memory_recall_here`
   still works post-compaction.

### CI gating

Default CI (`pytest -m "not integration and not slow"`) stays offline.
Integration suite (`-m memory` and `-m "memory and integration"`) runs nightly
or pre-merge for changes touching `agent/memory/`, `agent/compact.py`, or
`agent/builtin/{diary_write,set_wing,memory_recall_here}.py`.

## Out of scope (v2)

- Per-kind metadata filter (OBSERVATION vs DECISION) — requires vendoring
  `ingest_diaries`.
- Per-entry diary delete / `/diary-edit <date>`.
- Wing rename / merge.
- Multi-agent identity (currently `agent=kent` is hard-coded).
- Auto-reflection at session-end / every-N-turns.
- Wing-scoped wake-up at compaction time (token cost concern).
- Wing-scoped recording of turns (would require post-sweep wing-stamping
  of drawer metadata via direct ChromaDB upsert — kent's invariant is no
  direct ChromaDB writes).
