# Plan: Address underutilization in the kent memory system

## Context

A code review identified seven risks in kent's memory architecture (sweeper/tag two-pass, two ingest paths, mid-session compaction wing-blindness, no dedup, closet refresh has no trigger, transcripts unreachable, no recency decay). The user wants targeted fixes that close real silent-failure bugs and unlock dead features — without over-engineering, premature optimization, or breaking existing behavior.

After verifying every claim against the code (all confirmed) and probing fix surfaces, two of the seven risks deserve action now, one warrants a guardrail test, and four are deliberately deferred. Each deferral has a stated reason — they are not oversights.

## In-scope changes

### 1. Startup orphan reconciliation (closes the sweep/tag crash window)

**Why:** `record_turn()` (`agent/memory/mempalace_store.py:112-124`) sweeps drawers into ChromaDB inside `_SWEEP_LOCK`, then calls `_tag_session_drawers()` to add `wing`/`room` metadata. A process kill between sweep insert and tag write leaves drawers permanently without wing isolation — they'll surface in unrelated wings' recall results.

**Change:** Add a one-shot `reconcile_orphan_drawers()` method to `MemPalaceStore` that queries ChromaDB for drawers with id prefix `sweep_` whose metadata lacks a `wing` field, then tags them using the same logic as `_tag_session_drawers()`. The session_id embedded in the drawer id (`sweep_<session_id>_<n>`) tells us the original wing — look it up in the per-session wing record on disk. Drawers from sessions whose wing record is gone get tagged with a sentinel `wing="_orphan_unknown"` so they're isolated from real wings but still inspectable.

Call this once from `_repl()` in `agent/cli.py:1101` immediately after `MemPalaceStore()` construction, before the first `wake_up_full()` at line 1143.

**Files touched:**
- `agent/memory/mempalace_store.py` — add `reconcile_orphan_drawers()` (~30 lines, mirrors `_tag_session_drawers`)
- `agent/cli.py` — one call at startup (~2 lines)

**Reuses:** existing `_tag_session_drawers()` tagging logic, existing wing-record-on-disk read at `mempalace_store.py:83`.

---

### 2. Closet refresh on clean session exit (unlocks dead feature)

**Why:** Closets are LLM-summarized clusters of drawers. They only refresh when the user manually invokes the `closet_refresh` tool. Most sessions never call it — closets become stale or never populated, defeating the whole layer.

**Change:** Track a per-session turn counter in `MemPalaceStore` (incremented in `record_turn()`). In `_repl()` (`agent/cli.py:1089-1207`), wrap the main loop's normal exit paths (lines ~1192, 1205) in a `try/finally` that, on clean exit, calls a small helper `maybe_refresh_closets_on_exit()` which:

- Skips if turn counter == 0 (read-only session — nothing changed).
- Skips if `base_url`/`api_key` are not configured (avoids polluting the closet collection with regex-fallback summaries — the on-demand tool remains the user's escape hatch).
- Otherwise calls the same `regenerate_closets()` path that `ClosetRefresh.execute()` uses (`agent/builtin/closet_refresh.py:42-63`).

Do **not** add cron jobs, threshold heuristics, or background tasks. One opportunistic refresh per session is enough to keep closets warm without infrastructure.

**Files touched:**
- `agent/memory/mempalace_store.py` — turn counter increment, `maybe_refresh_closets_on_exit()` helper (~25 lines)
- `agent/cli.py` — `try/finally` wrap around exit paths (~5 lines)

**Reuses:** existing `regenerate_closets()` and `LLMConfig` construction logic from `agent/builtin/closet_refresh.py:48-56` — extract into a shared helper rather than duplicating.

---

### 3. Cross-ingest-path metadata contract test (guardrail, not a fix)

**Why:** Sweep-path drawers (`sweep_*`) and diary-path drawers (`drawer_diary_*`) get their metadata written by entirely separate code paths (`_tag_session_drawers` vs. `mempalace.diary_ingest.ingest_diaries`). They share recall surface but not write surface. A future mempalace upgrade can change one without the other and degrade recall silently. This is not theoretical — the prefix constants live in `agent/training/_palace_api.py:18-19`, but the metadata schemas have no shared contract.

**Change:** Add `tests/test_memory_ingest_parity.py` — one test that:

1. Records a turn (sweep path) in a temp palace.
2. Writes a diary entry (diary path) in the same palace.
3. Asserts both resulting drawers have the same set of required metadata keys (`wing`, `room`, plus whatever else both paths must agree on).

This is a few dozen lines of test code, no production change. It catches the "two paths drift" risk without trying to merge the paths — merging would be over-engineering.

**Files touched:**
- `tests/test_memory_ingest_parity.py` (new, ~50 lines)

---

## Explicitly out of scope (with reasons)

| Risk from review | Decision | Reason |
|---|---|---|
| Mid-session compaction uses global `wake_up()` not wing-scoped | **Skip** | `tests/test_compact.py:130-171` enforces this with documented rationale: wing diary stays recoverable via `memory_recall_here`, and bloating the compaction payload would defeat the purpose of compaction. Changing this breaks an existing intentional design. |
| No deduplication | **Skip** | No evidence of recall noise impacting users today. Adding canonicalization is a meaningful design change (similarity threshold? merge strategy? freshness winner?). Defer until there's a concrete recall failure to anchor the design on. |
| No recency decay | **Skip** | Doable in kent (drawer metadata could carry timestamps and recall could post-filter), but adds tunable parameters with no current signal that recency-blindness is hurting recall. Premature without evidence. |
| Transcripts write-only | **Skip** | Disk holds the truth — recoverable later with a small reader. No active user pain; this is a feature gap, not a silent bug. |

## Verification

1. **Orphan reconciliation:**
   - Unit: simulate a `sweep_*` drawer written without `wing` metadata, instantiate `MemPalaceStore`, assert reconciliation tagged it.
   - Manual: kill `kent` mid-`record_turn` (between sweep and tag), restart, verify the orphan got tagged on next launch.

2. **Closet refresh on exit:**
   - Unit: mock `regenerate_closets`, run a session that calls `record_turn` N times then exits cleanly, assert `regenerate_closets` was called once with the right config.
   - Unit: same setup but with no `base_url` configured — assert `regenerate_closets` was NOT called.
   - Unit: read-only session (zero `record_turn` calls) — assert no refresh.
   - Manual: run a real session with the LLM endpoint configured, exit cleanly, inspect `~/.kent/palace/` to confirm closets collection grew.

3. **Ingest parity test:** the test itself is the verification — `pytest tests/test_memory_ingest_parity.py`.

4. **Regression:** full `pytest tests/` must pass, especially `test_compact.py` and `test_memory_*.py` — these enforce the design choices we are explicitly preserving.

## Critical files

- `agent/memory/mempalace_store.py` — add `reconcile_orphan_drawers()`, turn counter, `maybe_refresh_closets_on_exit()`
- `agent/cli.py` — wire startup reconciliation + try/finally exit hook around `_repl()`
- `agent/builtin/closet_refresh.py` — extract LLMConfig/`regenerate_closets` invocation into a shared helper used by both the on-demand tool and the on-exit hook
- `tests/test_memory_ingest_parity.py` — new contract test
