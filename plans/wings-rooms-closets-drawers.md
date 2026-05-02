# Plan: Fully utilize MemPalace wings, rooms, drawers, closets

## Context

Kent currently uses ~3 mempalace submodules (`sweeper`, `layers`, `diary_ingest`) and ignores the rest. The result is a structural mismatch:

- **Turn drawers carry no wing or room.** `mempalace.sweeper.sweep()` writes only `{session_id, timestamp, message_uuid, role, source_file, filed_at, ingest_mode}` (verified at `mempalace/sweeper.py:274-282`). So `memory_recall_here` is diary-only and the bulk of memory (conversations) is untaggable.
- **No code/file drawers exist.** Kent talks about `loop.py`; the drawer is "what kent said about loop.py", never the file content. `mempalace.project_scanner.scan()` and `mempalace.room_detector_local.detect_rooms_local()` already do the wing/room auto-detection but nothing wires them in.
- **No closets are built.** `RESOURCE_BASELINES["closet_summary_policy"]` exists in `agent/training/apo_runner.py:33-36` but the runtime never invokes `mempalace.closet_llm.regenerate_closets` or reads `mempalace.palace.get_closets_collection()`. So that APO resource trains on a no-op signal.
- **The `scope_policy` APO resource is similarly starved** — there are no rooms to route between.

Goal: light up wings + rooms + closets on the live store and migrate existing palaces, so the APO resources kent already trains start optimizing real retrieval signal — without breaking the agent loop, the existing 196-test suite, the Discord gateway's per-channel wing scheme, or the Agent Lightning training pipeline. Add a 6th APO resource (`code_query_policy`) to handle the new code-drawer surface.

User-decided trade-offs (locked in):
- **Migration policy**: tag all old drawers `kent_default` first, then upgrade each session to a more specific wing if exactly one diary on the same date points there.
- **Closet flavor**: LLM-driven via the actor's configured endpoint (`closet_llm.regenerate_closets`). Upstream falls back to regex on failure.
- **Training scope**: add a 6th APO resource `code_query_policy` alongside feeding real signal to the existing 5.

---

## Phase A — Tag turn drawers with wing + room (unlocking change)

**Why first**: makes every other phase's signal actually retrievable. Today `Layer3.search(wing=X)` over conversation drawers returns nothing because no conversation drawer carries `wing` metadata.

**Files**:
- `agent/memory/mempalace_store.py:80-90` (extend `record_turn`)

**Change**: after `sweeper.sweep(...)` succeeds inside `record_turn`, immediately post-tag the new drawer IDs with `{wing: self._active_wing, room: room}`.

```python
# new helper at module scope
def _tag_session_drawers(palace_path, session_id, message_uuids, *, wing, room):
    from mempalace.palace import get_collection
    from mempalace.sweeper import _drawer_id_for_message
    col = get_collection(str(palace_path), create=False)
    ids = [_drawer_id_for_message(session_id, u) for u in message_uuids]
    existing = col.get(ids=ids, include=["metadatas"])
    merged = []
    for m in (existing.get("metadatas") or []):
        m = dict(m or {})
        m["wing"] = wing
        m["room"] = room
        merged.append(m)
    if merged:
        col.update(ids=existing.get("ids") or [], metadatas=merged)
```

`record_turn` collects message UUIDs from the messages it just appended and calls `_tag_session_drawers` under the same `_SWEEP_LOCK`. Default room: `"conversation"`. The Discord gateway already sets a per-channel wing via `set_active_wing`; nothing else changes for it.

**Reuse**: `mempalace.sweeper._drawer_id_for_message` (deterministic ID — relied on by sweeper itself for idempotency, safe to import). `mempalace.palace.get_collection`.

**Idempotency**: re-ingesting the same JSONL is safe — `col.update(ids, metadatas)` with the same `wing`/`room` is a metadata no-op at the chroma layer.

**Backwards compatibility**: extra metadata fields are additive; existing readers ignore unknown keys. `Layer3.search` with no wing/room filter behaves identically.

---

## Phase B — Code/file drawers + LLM closet refresh

**Files (new)**:
- `agent/builtin/code_drawer.py`
- `agent/builtin/closet_refresh.py`
- `agent/builtin/__init__.py` (export both)
- `agent/cli.py` (register new tools in `build_registry`; add `kent index` subcommand; add `/rooms`, `/closets` slash commands)

**`code_drawer` tool** — wraps `mempalace.palace.get_collection().add(...)` directly with `{wing, room, source_label="code", path, language}` metadata. **Not concurrency-safe** (`is_concurrency_safe = False`) — same SQLite contention as `diary_write`. Pydantic args: `path: str, language: str | None = None, wing: str | None = None, room: str | None = None`. Defaults: wing = active wing; room = parent dir name. Truncate at 100 KB.

**`closet_refresh` tool** — wraps `mempalace.closet_llm.regenerate_closets(palace_path, wing=wing, cfg=LLMConfig(endpoint=..., model=..., key=...))`. The cfg is built from kent's resolved actor service (Atlas Cloud by default) using the existing credential resolution path in `agent/cli.py`. **Default `wing=None`** — `regenerate_closets(wing="X")` filters drawers by `meta.wing == "X"`, which skips every pre-migration drawer (`meta.wing == ""`). The first global rebuild must run with no wing filter; wing-scoped refresh becomes meaningful only after Phase C migration tags old drawers. Pydantic args: `wing: str | None = None`. On missing LLM config, upstream auto-falls-back to `palace.build_closet_lines` (regex), which keeps it safe in offline/test contexts. Not concurrency-safe.

**`kent index <path>` CLI subcommand** — read-only walk over a working tree:
1. `project_scanner.scan(root)` → take the strongest project as wing name (call `set_active_wing` if `--set-wing`).
2. Walk source files (respect `.gitignore`), call the new `code_drawer` add path with `room = parent_dir.name`. Skip binary files via the existing skip pattern in `project_scanner.SKIP_DIRS`.
3. After ingest, call `closet_refresh(wing=<project>)` once.

> **Deferred (was step 2)**: `room_detector_local.detect_rooms_local()` would be the "smart room map" upgrade — but it writes a `mempalace.yaml` to the user's project root and pulls in interactive paths. v1 uses parent-dir names, which is enough signal for retrieval. Revisit only when wake-up `recall_a2` plateaus and rooms become the obvious next lever.

**Slash commands** (`agent/cli.py`):
- `/rooms` — list rooms in active wing by reading kent's palace directly: aggregate `meta.room` counts via `palace.get_collection(memory_store.palace_path).get(where={"wing": active_wing}, include=["metadatas"])`. (Do **not** call `mcp_server.tool_list_rooms` — it reads `_config.palace_path` (`~/.mempalace/palace`), not kent's `~/.kent/palace`.)
- `/closets` — invoke `closet_refresh` for the active wing.

**`_build_system_prompt`** (`agent/cli.py:120-179`): when active wing has known rooms, append a one-liner like `wing 'X' has rooms: [code, conversation, daily]`. No-op for fresh installs.

---

## Phase C — One-time migration script

> **Order matters**: this used to be Phase D. Migrating *before* the new APO resource ships means Phase D's `code_query_policy` rollouts have wing-tagged drawers to sample. Without migration first, every old conversation drawer has `wing == ""` and the new reward signal collapses to 0.

**Files (new)**:
- `agent/migrate.py`
- `agent/cli.py` (hook `kent migrate-palace` subcommand)
- `tests/test_migrate_palace.py`

**CLI**: `kent migrate-palace [--dry-run] [--default-wing kent_default] [--regenerate-closets]`

**Algorithm** (idempotent — re-runnable):
1. Walk `~/.cache/kent/transcripts/*.jsonl` (kent's durable JSONL buffer).
2. For each transcript, derive `(session_id, [message_uuids], session_date)` via `mempalace.sweeper.parse_claude_jsonl`.
3. Compute drawer IDs via `_drawer_id_for_message`. Fetch existing metadata in batch via `get_collection().get(ids=...)`.
4. Skip any drawer that already has a wing (idempotency).
5. **Wing inference**:
   - List diaries in `~/.kent/diaries/<wing>/<session_date>.md`. If exactly one wing has a diary on that date, assign that wing. Otherwise assign `--default-wing` (default `kent_default`).
   - Always assign `room = "conversation"` and add `migrated_at: <iso>`.
6. Bulk `collection.update(ids=..., metadatas=...)`.
7. **Graph backfill**: after all updates, call `mempalace.palace_graph.invalidate_graph_cache()` once. The next `kent viz` snapshot tick will rebuild `build_graph()` against the now-tagged drawers, so previously-orphaned conversation drawers show up under their inferred wing's `room:conversation` node — no separate "create graph nodes" step is needed because the snapshot builder is purely metadata-driven (`agent/viz/snapshot.py:30-78`). Without this call, users wait up to 60s for the cache TTL to expire before the viz reflects the migration.
8. After all sessions, if `--regenerate-closets` is passed, run `closet_refresh` once per wing touched (also invalidates the cache as a side effect of writes).

**Reuse**: `mempalace.sweeper.parse_claude_jsonl`, `mempalace.sweeper._drawer_id_for_message`, `mempalace.palace.get_collection`, `mempalace.palace_graph.invalidate_graph_cache`. No new mempalace dependencies.

**Dry-run**: reports `{sessions_scanned, drawers_to_update, wings_inferred: {wing: count}, graph_nodes_unlocked: <int>}` without writing. The `graph_nodes_unlocked` count is the number of distinct (wing, room) pairs that will become visible in `kent viz` after the real run — derived from `wings_inferred` × {`"conversation"`}, deduped.

---

## Phase D — Agent Lightning: feed real signal + add 6th APO resource

> **Prerequisite**: Phase C must have run on the user's palace first — the new `code_query_policy` resource samples wing-tagged code drawers, and `recall_game_rollout`'s wing-scoped reward only fires when tasks carry a wing.

**Files**:
- `agent/training/palace_isolation.py:21-50` (extend snapshot)
- `agent/training/apo_runner.py:9-37` (add resource)
- `agent/training/rollout.py:182-330` (new agent builder, register new tools in registry)
- `tests/training/test_palace_isolation.py` (extend fixture for closets/tunnels)
- `tests/training/test_code_query_rollout.py` (new)

**Snapshot extension** — closets live inside `chroma.sqlite3` (same DB as drawers — verified by `palace.get_closets_collection` using `get_collection(palace_path)` under the hood), so the existing `shutil.copy2(src, dst)` for `chroma.sqlite3` at line 32 already brings closets along. Two real gaps to close:
1. **Tunnels JSON**: copy `~/.mempalace/tunnels.json` into the scratch if it exists. The current snapshot copies `~/.kent/diaries` and `active_wing.txt` but tunnels live under `~/.mempalace/`. Without this, `tunnel_create` writes from a rollout silently leak into the real tunnels file. **Constraint**: `palace_graph._TUNNEL_FILE` is hardcoded as a module-level constant at import time — there's no env-var or `MEMPALACE_HOME` knob to redirect it. So: copy `tunnels.json` into the scratch for inspection/assertion purposes, and tag the rollout's `TunnelCreate` tool as a no-op-during-training (skip the `create_tunnel` call when `_active_config` is set). Don't waste cycles trying to redirect the path.
2. **`assert_isolation`** gains an inode check for `tunnels.json` mirroring the SQLite check at lines 62-66 — catches future regressions even though we currently no-op the writer.

**Tools registered in rollout** (`rollout.py:113-121`): add `CodeDrawer` and `ClosetRefresh` so the actor can exercise the full memory surface during APO rollouts. Without them, optimization can't discover prompts that use code drawers or closets.

**`recall_game_rollout`** (`rollout.py:250-330`): when the sampled task carries a wing, switch to `Layer3.search_raw(query, wing=wing, room=room, n_results=1)` so the reward reflects wing-scoped recall (mirrors the existing `recall_a2` metric in `recall_games.py`). Reward formula unchanged.

**6th APO resource — `code_query_policy`**:
- Add to `RESOURCE_ORDER` (apo_runner.py:9-15) after `query_rewrite_policy`.
- Baseline in `RESOURCE_BASELINES`: `"Phrase a question to retrieve this code drawer: name the symbol, file, or behavior the snippet implements."`
- New `build_code_query_apo_agent()` in `rollout.py` — clone of `build_recall_game_apo_agent()` but the task sampler filters drawers where `metadata.room == "code"` (or `source_label == "code"`). Reward: cosine sim of generated query → source code drawer via `Layer3.search_raw(query, room="code", n_results=1)`.
- `kent train --resource code_query_policy` works automatically once the resource is in `RESOURCE_ORDER` (apo_runner is generic).

**Sequential freezing** (rollout.py:37-56) keeps working: `_load_frozen_resources` already excludes the active resource and concatenates the rest, so the new resource slots into the existing sequence without code changes.

---

## Phase E — Backwards compatibility checks

- **Existing `MemPalaceStore` API** unchanged — `record_turn` keeps its current signature, just adds an optional `room: str = "conversation"` kwarg.
- **`tests/conftest.py`** autouse `NullMemoryStore` fixture remains intact (it monkeypatches `_default_store`); no test touches the new tagging path unless explicitly opted in.
- **Discord gateway** keeps working — `agent/gateway.py` already calls `set_active_wing(per_channel_name)` per channel, and Phase A reads `self._active_wing` at `record_turn` time.
- **`kent doctor`** — extend `[memory]` block with one new line: `rooms_in_active_wing`. Closet count, tunnel count, room×wing matrix, etc. are easy adds later when a real diagnostic question motivates them — don't pre-build them.
- **Lazy imports** — every new mempalace import lives inside function bodies (matches existing `MemPalaceStore` convention) so `import agent` stays cheap.

---

## Phase F — Memory graph (kent viz) integration

The 3D graph already renders all five node types — `identity`, `wing`, `room`, `drawer`, `closet` — plus `tunnel` / `passive_tunnel` / `closet_ref` / `orphan` link types (verified at `agent/viz/snapshot.py:30-178`). The snapshot builder is metadata-driven: it reads the same Chroma collection that Phases A–C are writing to, so wings/rooms/drawers/closets created by this plan **appear automatically** the next time the snapshot rebuilds. Three real touches are still needed:

### 1. `agent/viz/snapshot.py` — distinguish code drawers visually

Today drawers are colored by `meta.kind` (OBSERVATION/FINDING/DECISION/PATTERN). Code drawers won't carry a `kind`, so they fall through to the default `#bbb` and become indistinguishable from un-categorized conversation drawers. Add a `source_label` route ahead of the kind lookup at line 87:

```python
source_label = (meta or {}).get("source_label", "")
if source_label == "code":
    kind_color = "#fc6"   # warm amber for code drawers
    val_boost = 0.3       # slightly larger so they stand out in dense rooms
else:
    kind_color = {
        "OBSERVATION": "#5af", "FINDING": "#5f8",
        "DECISION": "#fa5", "PATTERN": "#a5f",
    }.get((meta or {}).get("kind", ""), "#bbb")
    val_boost = float((meta or {}).get("importance", 1.0)) * 0.4
```

### 2. `agent/viz/snapshot.py` — better closet labels

The current closet label is `(doc or "")[:40]`, which on LLM-generated closets surfaces the raw `topic|entities|→drawerIds` line. Parse the first line and take the topic token:

```python
first_line = (doc or "").splitlines()[:1]
topic = first_line[0].split("|", 1)[0].strip() if first_line else ""
label = topic[:40] or "closet"
```

This is cosmetic-but-load-bearing: with hundreds of closets in the graph, unreadable labels make the layer useless.

### 3. Cache invalidation hook (write-side)

`mempalace.palace_graph._GRAPH_CACHE_TTL = 60.0`. None of the existing write paths (`sweep`, `palace.add`, `closet_llm.regenerate_closets`) call `invalidate_graph_cache()` — verified by grep. So new wings/rooms/drawers take up to 60 s to surface in `kent viz`. For batch operations the user explicitly triggered, that lag is wrong. Call `mempalace.palace_graph.invalidate_graph_cache()` once at the end of:

- **Phase A** `_tag_session_drawers` — only after the *first* turn of a session in a wing the palace hasn't seen (cheap idempotency check: skip if `wing` already appears in `build_graph()`'s cached result).
- **Phase B** `code_drawer.call` — once at the end of `kent index` (NOT per file — would thrash the cache).
- **Phase B** `closet_refresh.call` — at the end (closets get their own collection but live nodes still benefit from a full refresh).
- **Phase C** `kent migrate-palace` — once after the bulk update loop (already covered in Phase C step 7).

Lazy import inside each call site — same pattern as the rest of the kent → mempalace surface.

### 4. Frontend: HUD breakdown by source_label (optional, small)

`agent/viz/static/index.html:467` reads `data.nodes.filter(nn => nn.type === 'drawer').length`. Split it:

```javascript
const drawers = data.nodes.filter(nn => nn.type === 'drawer');
const code = drawers.filter(d => d.source_label === 'code').length;
document.getElementById('d').textContent = drawers.length + ` (code: ${code})`;
```

Requires snapshot.py to pass `source_label` through on the drawer node dict. Two-line change in both files. Skip if you want to land Phase F minimal — the visual color cue in §1 already conveys the same information.

### What we deliberately do NOT add

- **No new node `type`**: code drawers are still `type: "drawer"` — adding a new type would require frontend force-graph styling, link rules, and breaks `data.nodes.filter(nn => nn.type === 'drawer')` consumers. Color via metadata is enough.
- **No new link type for code → room**: existing `room → drawer` link wiring (snapshot.py:101-102) handles them as long as `meta.room` is set.
- **No graph-side migration logic in `kent viz`**: the viz is a read-only renderer; the migration backfill happens on the Chroma side via Phase C's `col.update`, and the viz picks it up on the next snapshot tick.

### Verification

- `kent index .` then `kent viz` → expect amber drawer cluster orbiting the active wing's `room:agent`, `room:builtin`, `room:training`, etc. (one per top-level dir).
- `kent migrate-palace --regenerate-closets` then `kent viz` → expect previously-orphaned drawers (the gray `orphan` links from `identity`) to relocate under their inferred wings' `room:conversation` node within ~1 s of migration completing (cache invalidate is immediate).
- Snapshot test (`tests/viz/test_snapshot.py`): seed a Chroma collection with `{wing: "x", room: "code", source_label: "code"}` drawers and assert the resulting node has `color == "#fc6"`.

---

## Critical files (modify)

| Path | Change |
|---|---|
| `agent/memory/mempalace_store.py:80-90` | Tag new drawers with `wing`+`room` after sweep; invalidate graph cache on first turn in a new wing |
| `agent/builtin/__init__.py` | Export `CodeDrawer`, `ClosetRefresh` |
| `agent/cli.py:120-179` | Surface rooms in system prompt; new slash commands; `kent index` and `kent migrate-palace` subcommands |
| `agent/training/palace_isolation.py:21-77` | Snapshot tunnels.json; extend `assert_isolation` |
| `agent/training/apo_runner.py:9-37` | Add `code_query_policy` to `RESOURCE_ORDER` + baseline |
| `agent/training/rollout.py:113-121, 234-330` | Register new tools in rollout registry; add `build_code_query_apo_agent`; wing-scope `recall_game_rollout` reward |
| `agent/viz/snapshot.py:86-96, 124-145` | Color code drawers by `source_label`; parse closet topic for label |
| `agent/viz/static/index.html:467` *(optional)* | Split HUD drawer count: `total (code: N)` |

## Critical files (new)

| Path | Purpose |
|---|---|
| `agent/builtin/code_drawer.py` | `add_code_drawer` tool — wraps `palace.get_collection().add` |
| `agent/builtin/closet_refresh.py` | `closet_refresh` tool — wraps `closet_llm.regenerate_closets` |
| `agent/migrate.py` | `kent migrate-palace` implementation |
| `tests/test_migrate_palace.py` | Migration unit test (synthetic transcripts + diary) |
| `tests/test_record_turn_tags_wing.py` | Verify Phase A tagging |
| `tests/training/test_code_query_rollout.py` | 6th APO resource rollout test (fake LLM, fake palace) |

---

## Verification

1. **Existing suite stays green**:
   ```bash
   uv run pytest -m "not integration and not memory and not slow"
   ```
   All currently-collected tests (~352 today) must pass with zero modification.

2. **Phase A unit test** (`tests/test_record_turn_tags_wing.py`):
   - Build a `MemPalaceStore` against a tmp palace, set active wing to `myproj`.
   - Call `record_turn` with two messages.
   - Assert `get_collection(palace).get(ids=[expected_ids])` returns metadata with `wing="myproj"`, `room="conversation"`.

3. **Migration unit test** (`tests/test_migrate_palace.py`):
   - Seed two transcripts (different `session_id`, both dated `2026-04-15`).
   - Seed one diary at `~/.kent/diaries/projA/2026-04-15.md`.
   - Run `kent migrate-palace`. Assert session #1 (matching diary date) → `wing="projA"`, session #2 → `wing="kent_default"`.
   - Re-run; assert no updates (idempotency).

4. **Training pipeline still trains**:
   ```bash
   uv run pytest tests/training/ -m "not integration and not live_apo"
   ```
   25 tests must stay green. New `test_code_query_rollout.py` adds 1+.

5. **6th APO resource end-to-end** (live, `live_apo` marker, ~5 min) — **requires `kent migrate-palace` to have run first** (Phase C), otherwise no code drawers carry a wing and the rollout sampler returns empty:
   ```bash
   kent train --resource code_query_policy \
       --pair qwen/qwen3.6-35b-a3b+qwen/qwen3.6-35b-a3b \
       --apo-base-url https://api.atlascloud.ai/v1 \
       --gradient-model qwen/qwen3.6-35b-a3b \
       --apply-edit-model qwen/qwen3.6-35b-a3b \
       --rounds 1 --runners 1 --train-size 3 \
       --skip-collusion-check
   ```
   Verify `lightning_store/resources/code_query_policy.txt` is written with a non-empty optimized prompt.

6. **Manual smoke test**:
   ```bash
   kent index .                    # ingest this repo
   kent                            # REPL
   /rooms                          # should list detected rooms
   you> what does mempalace_store.py do?
   # Expect actor to call memory_recall_here, hit the indexed code drawer.
   /closets                        # build closets via LLM (or regex fallback if no key)
   ```

7. **Migration on real data** (after the above):
   ```bash
   kent migrate-palace --dry-run    # report counts
   kent migrate-palace --regenerate-closets
   kent doctor                      # verify rooms_in_active_wing > 0
   ```

8. **Visual confirmation** (`kent viz` running in a second terminal):
   - **Before any work**: HUD shows wings + diary files; conversation drawers all hang as gray `orphan` links from the `identity` node (because they have no `wing` metadata).
   - **After Phase A** (one new turn in a wing): the new turn's drawers attach to `room:conversation` under the active wing within ~1 s of cache invalidation.
   - **After `kent index .`**: amber drawer clusters appear under `room:agent`, `room:builtin`, etc. for the active wing.
   - **After `kent migrate-palace`**: previously-orphaned conversation drawers re-anchor under their inferred wings' `room:conversation` node — the `identity → drawer` orphan links collapse and become `room → drawer` links.
   - **After `closet_refresh`**: closet nodes appear with readable topic labels (not raw `topic|entities|→ids` strings) and `closet_ref` links to their drawers.

---

## Known follow-ups (do NOT fix in this plan)

- **Closet ID collision** — `closet_llm.regenerate_closets` builds `closet_id_base = f"closet_{w}_{r}_{os.path.basename(source)[:30]}"`. Every kent transcript is filed with `source_label="kent"`, so all wing-scoped conversation closets collapse onto a single deterministic ID and overwrite each other on each rebuild. This is a latent mempalace bug, not introduced by this plan. Re-surface as a separate ticket once the wing/room signal is live and the collision starts costing recall.
- **Smart room detection for `kent index`** — `room_detector_local.detect_rooms_local()` was deferred from Phase B. Revisit only when wake-up `recall_a2` plateaus and parent-dir-as-room is the bottleneck.
- **Phase C tiebreaker** — when more than one wing has a diary on the same date, migration falls back to `kent_default`. Acceptable for v1; could rank by most-recent diary mtime later if signal loss matters.
