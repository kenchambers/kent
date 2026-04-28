# Plan: Self-Learning Kent via Agent Lightning + MemPalace

## Context

**Why:** kent is a Python asyncio agent already wired to MemPalace for persistent memory. We want kent to *self-improve* — both in task performance and in how accurately it recalls its own memory — without sacrificing the local-first inference path. Microsoft Agent Lightning supplies the training framework; APO (Automatic Prompt Optimization) is the algorithm we'll lead with because it needs no GPU, iterates in minutes, and consumes free-text feedback (which our critic naturally produces).

**Approach in one sentence:** Two parallel APO optimization tracks — one over actor task performance scored by a cross-family critic, one over recall behavior scored by self-supervised palace-as-its-own-trainer games — both swappable across actor/critic model pairs in a full sweep.

**Key alignment with existing code:**
- kent's `MemoryStore` Protocol (`agent/memory/store.py`) is the natural per-rollout isolation seam. Construct `MemPalaceStore(palace_path=scratch_palace, kent_home=scratch_home)` per rollout, hand to `loop.run(memory_store=...)`. No env-var manipulation.
- kent's `WingedMemoryStore` extension (`agent/memory/store.py:26`) is the seam for Track 2: tools that need wing capability isinstance-check it; training games consume the same surface.
- kent's `LLM` Protocol (`agent/llm.py:15-28`) makes swap-pair trivial — two `OpenAICompatibleLLM` instances with different `(base_url, model)`.
- kent's existing `critic.py` already separates critic concerns and accepts a distinct LLM. APO reward flows through it.
- MemPalace integration is layered: `mempalace.sweeper.sweep` (transcript ingest), `mempalace.diary_ingest.ingest_diaries` (diary ingest), `mempalace.layers.MemoryStack` (status/wake-up/recall), `mempalace.layers.Layer3` (filtered search with `wing=`/`room=`). Track 2 games consume these directly AND through kent's actual tool surface (`diary_write`, `memory_recall_here`, `set_wing`) since those are what the live actor uses.

---

## Architecture

### Two parallel optimization tracks

**Track 1 — Task performance.** `@agl.rollout` wraps `loop.run`. Per rollout: snapshot palace → run actor → critic scores rollout → reward returned to APO → APO rewrites the actor's prompt template.

**Track 2 — Recall accuracy (self-supervised).** Background `kent wake-up` runs four games against the live palace:
- *Game A — recall games:* sample drawer → ask actor "what question would retrieve this?" → run `Layer3.search()` on that question → reward = recall@5 (drawer ID hit). Two variants run side-by-side: A1 unscoped (`Layer3.search(query)`) trains `query_rewrite_policy`; A2 wing-scoped (`Layer3.search(query, wing=W, room="daily")` — kent's actual `recall_in_wing` path) trains the same policy under wing constraints. Both contribute to APO updates.
- *Game B — counterfactual scoping:* replay real-traffic queries at three scopes (none / wing / wing+room=daily, mirroring kent's actual `recall` vs `recall_in_wing` split) → critic picks best → label trains `scope_policy`. The `scope_policy` resource governs which retrieval tool the actor reaches for (`memory_recall` vs `memory_recall_here`).
- *Game C — closet fidelity:* pick a closet → generate a question whose answer is in its drawer → can actor answer from closet alone? Trains `closet_summary_policy`. Closets are now built over a mix of transcript-ingested and diary-ingested drawers (kinds: OBSERVATION/FINDING/DECISION/PATTERN); evaluation stratifies by drawer source so we can detect summary-quality regressions specific to diaries.
- *Game D — tunnel utility (logging only, no APO):* during Track 1 rollouts, log whether followed tunnels yielded cited content. Surface in `kent doctor`.

Deferred to v2 (called out so we don't reinvent it later): a `diary_write_policy` resource trained by closing the loop between Track 1 (rollouts that call `diary_write`) and Track 2 Game A (subsequent recall hits on that drawer). Requires drawer-id provenance from `diary_ingest` we don't currently surface — out of scope for v1.

### Swap-pair: full sweep

Every (actor × critic) combo runs an independent APO training. `~/.kent/swap_pairs.toml` lists actors and critics; sweep coordinator iterates the cross product. Same-family pairings rejected at config load (collusion risk). Cross-critic consensus check every 10 rounds detects drift.

### Resources (Lightning-managed, optimized one at a time)

| Resource | Optimized by | Initial baseline |
|---|---|---|
| `actor_system_prompt` | Track 1 | kent's existing system prompt |
| `retrieval_policy` | Track 1 + 2 | "When to call memory_recall: …" |
| `query_rewrite_policy` | Track 2 Game A | "Phrase a question to retrieve this drawer: …" |
| `scope_policy` | Track 2 Game B | "Decide search scope: …" |
| `closet_summary_policy` | Track 2 Game C | "When writing a closet, preserve answerability of: …" |
| `critic_rubric` | Track 1 (rare; mostly frozen) | task_success / reasoning / tool_eff / memory_use |

Sequential optimization order: actor_system_prompt → retrieval_policy → scope_policy → query_rewrite_policy → closet_summary_policy. Each round freezes prior resources.

### Reward shape

Critic returns JSON:
```json
{ "task_success": 0|1, "reasoning_quality": 0..1, "tool_efficiency": 0..1, "memory_use": 0..1, "rationale": "..." }
```
Scalar reward (APO requirement, [0,1]):
`0.5*task_success + 0.2*reasoning + 0.15*tool_eff + 0.15*memory_use`
The `rationale` string is forwarded via `agl.emit_object({"feedback_text": rationale})` — APO's textual-gradient step consumes it directly.

### Per-rollout isolation (palace + diaries + active wing)

A rollout's scratch must isolate everything `MemPalaceStore` reads/writes, not just the palace. Three regions, three rules:

1. **`~/.kent/palace/`** — hardlink every file EXCEPT `chroma.sqlite3`, which is `shutil.copy2`'d. SQLite in-place writes through a hardlink would corrupt the source.
2. **`~/.kent/diaries/`** — full copy via `shutil.copytree(symlinks=False)`. Diaries are append-only Markdown protected by `fcntl.flock` (`agent/memory/diary.py:35-42`); `flock` is advisory and does NOT prevent hardlink-backed cross-contamination, so writes from a rollout's `diary_write` tool MUST NOT reach the base `~/.kent/diaries/`. Cost is negligible — these are small text files.
3. **`~/.kent/active_wing.txt`** — `shutil.copy2`. One-line file; the rollout may switch wings via `set_wing` and must not mutate the base.

Layout: `~/.mempalace/_rollouts/<rollout_id>/{palace, diaries, active_wing.txt}`. Construct `MemPalaceStore(palace_path=<scratch>/palace, kent_home=<scratch>)` so `DiaryWriter` resolves `kent_home / "diaries" / <wing>` against the scratch root (see `mempalace_store.py:127-135`). Cleanup on rollout exit. `MEMPALACE_PALACE_PATH` env var unused.

Hardlinks keep the palace snapshot O(files), not O(bytes) — sub-second cold start. The diary copy is bounded by `O(wings × days)` of small markdown — practically free.

### Rollout boundary

A rollout = one task. New tools `task_start(task_id, prompt)` / `task_end(task_id, outcome)` mark boundaries. Zero runtime cost; their invocations are the signal. Sentinel tokens and idle-timeout heuristics rejected for being non-deterministic.

---

## Module layout

### New files

```
agent/training/
  __init__.py              # TrainingConfig dataclass
  rollout.py               # @agl.rollout-decorated kent_task_rollout
  palace_isolation.py      # snapshot()/cleanup() helpers
  critic_scorer.py         # critic LLM call + JSON parse + scalar reward
  swap_pair.py             # SwapPair, sweep coordinator, family-collision guard
  apo_runner.py            # train_resource() — wraps agl.Trainer.fit()
  recall_games.py          # Game A
  scope_eval.py            # Game B
  closet_fidelity.py       # Game C
  tunnel_utility.py        # Game D logger
  datasets.py              # TrainingExample + loaders
  eval_harness.py          # held-out + collusion probes + consensus check

agent/builtin/
  task_boundary.py         # task_start, task_end tools

tests/training/
  test_rollout.py
  test_palace_isolation.py # incl. SQLite-corruption regression
  test_critic_scorer.py
  test_recall_games.py
  test_swap_pair.py        # incl. same-family rejection test
```

### Files to modify

- `agent/cli.py` — extend `StartupChoice` with training fields; add `kent train` and `kent wake-up` subcommands. Touch points: `StartupChoice` (lines 140-148), `gather_startup_choice` (252-316), `cmd_run`/`cmd_repl` (721-805), `_build_parser` subparser block (1001-1038), `main()` dispatch (1043-1048). Both `cmd_run` and `_repl` already construct `MemPalaceStore` and register the wing/diary tools (`cli.py:649-672` and `cli.py:764-786`) — training mode reuses this construction path with scratch paths injected, not a parallel one.
- `agent/builtin/__init__.py` — register `task_start`/`task_end` alongside the eight existing tools (`Spawn`, `WebSearch`, `WebFetch`, `Shell`, `MemoryRecall`, `MemoryRecallHere`, `DiaryWrite`, `SetWing`).
- `pyproject.toml` — add `agentlightning>=0.1` (verify package name on PyPI before pinning).

### Reuse (do not modify)

- `agent.loop.run` (`agent/loop.py:59`) — pass `memory_store=` and `system=` per rollout
- `agent.memory.mempalace_store.MemPalaceStore` (`agent/memory/mempalace_store.py:30`) — accepts `palace_path` AND `kent_home` parameters (`__init__` at lines 33-46); both are our isolation seams (palace for vectors/sqlite, kent_home for diaries + active_wing.txt)
- `agent.memory.store.WingedMemoryStore` (`agent/memory/store.py:26`) — Protocol contract Track 2 games consume
- `agent.memory.wings` (list_wings, read_active_wing, read_intent, sanitize_wing) — Track 2 Game B uses real wing structure to seed scope-eval scenarios
- `agent.memory.diary.DiaryWriter` (`agent/memory/diary.py:15`) — Track 2 Game C and v2 `diary_write_policy` consume the same ingest path; do not bypass it
- `agent.cli.build_registry` (`agent/cli.py:321`) — base tool registry builder; training rollouts re-register memory/wing tools the same way `_repl` does at `cli.py:665-670`
- `agent.cli._build_system_prompt` (`cli.py:87-135`) — composes system prompt with active-wings list; APO's `actor_system_prompt` resource replaces the `_SYSTEM_PROMPT_BASE` constant (`cli.py:67-81`), NOT this composition function
- `agent.llm.OpenAICompatibleLLM` (`agent/llm.py:31`) — instantiated twice per rollout (actor + critic), parameterized by SwapPair
- `mempalace.layers.Layer3` — Track 2 Game A retrieval (already used in `mempalace_store.py:110` for global, `mempalace_store.py:120` for wing-scoped)
- `mempalace.layers.MemoryStack` — used for `wake_up` and status (`mempalace_store.py:78`, `mempalace_store.py:91`); Track 2 reuses for closet enumeration
- `mempalace.sweeper.sweep` — transcript ingest through scratch palace (already used in `mempalace_store.py:69`)
- `mempalace.diary_ingest.ingest_diaries` — diary ingest through scratch palace (already used via `DiaryWriter._ingest`, `agent/memory/diary.py:46-58`)
- `agent.critic` — pattern for second-LLM invocation; we mirror its construction style in `critic_scorer.py`

---

## Critical risks

1. **Hardlinked SQLite + diary corruption.** Two distinct files MUST NOT be hardlinked from the base:
   - `~/.kent/palace/chroma.sqlite3` — SQLite in-place writes propagate through hardlinks. Use `shutil.copy2`.
   - `~/.kent/diaries/<wing>/<date>.md` — appended under `fcntl.flock` (`agent/memory/diary.py:35-42`); `flock` is advisory and does not break hardlink sharing, so a rollout's `diary_write` would mutate the base. Use `shutil.copytree` for the whole `diaries/` tree.
   Mitigation: explicit branches in `palace_isolation.snapshot()` + startup self-check that asserts inode inequality for sqlite AND for a sample diary file. Regression tests: `test_palace_isolation.py::test_sqlite_writes_dont_corrupt_base` and `::test_diary_writes_dont_corrupt_base`.

2. **Critic collusion (reward hacking).** Same-family actor/critic pairs let the actor learn to flatter the critic. Mitigation: family-collision guard rejects same-family pairs at config-load time; mandatory collusion probe (5 deliberately-wrong outputs); cross-critic consensus check every 10 rounds (third critic from a different family — alert if rank correlation drifts >0.3).

3. **APO breaks local-first promise during training.** `agl.APO(AsyncOpenAI())` calls OpenAI to do prompt rewriting, regardless of who the actor is. Acknowledged tradeoff. Inference stays 100% local; *training* requires `OPENAI_API_KEY`. Document prominently. Future work: local-LLM textual-gradient implementation if/when one ships.

4. **Concurrent palace writes from parallel runners.** kent's README already flags subagent-write races as unverified; `n_runners=4` multiplies risk. Mitigation: each rollout gets its own scratch palace AND its own `MemPalaceStore` instance — no shared state across rollouts. `Spawn` inside a rollout shares only that rollout's scratch. Stress test: `tests/training/test_palace_isolation.py::test_parallel_rollouts_dont_corrupt`.

---

## Verification

### 1. Unit (offline, no LLM)
```
cd /Users/kennethchambers/Documents/GitHub/kent
uv run pytest tests/training/ -m "not integration"
```
Asserts: scratch snapshot/restore, sqlite copy branch, **diary copy branch (no hardlinks)**, critic JSON parsing, scalar reward math, swap-pair family-collision rejection, dataset loading.

### 2. Integration smoke (FakeLLM)
```
uv run pytest tests/training/test_rollout.py -v
```
Mocks both actor and critic with deterministic FakeLLM. Asserts: `kent_task_rollout` runs end-to-end, scratch palace created+cleaned, reward in [0,1], OtelTracer captures spans.

### 3. One real APO round (cheap)
```
OPENAI_API_KEY=... uv run kent train \
    --resource actor_system_prompt \
    --pair gpt-5-mini+gpt-5-mini \
    --rounds 1 --runners 2 --train-size 10
```
Asserts: `Trainer.fit` completes, optimized resource saved to `~/.kent/lightning_store/resources/`, val reward computed and ≥ baseline.

### 4. Recall game smoke
```
# Seed palace with known drawers first via existing kent record_turn flow.
uv run kent wake-up --duration 2m
cat ~/.kent/lightning_store/metrics/*.jsonl | tail -n 20
```
Asserts: recall@5 metric logged, optimized `query_rewrite_policy` differs from baseline.

### 5. Collusion probe regression
Seed eval set with 5 known-bad-but-plausible actor outputs; run one round.
Asserts: `critic_pass_rate` on probes ≤ 1/5. >1/5 fails the gate (swap pair colluding — config rejected).

### 6. Held-out drift check
Run 5 APO rounds on `actor_system_prompt`.
Asserts: |val_reward − held_out_reward| < 0.1 across rounds. Larger gap = overfitting to the critic.

### 7. Full-sweep dry run (small)
```
uv run kent train sweep --resource actor_system_prompt \
    --rounds 1 --train-size 5 --runners 2
```
With 2 actors × 2 critics defined, asserts 4 independent training runs complete and produce 4 distinct optimized prompts on disk.

---

## Out of scope (v1)

- VERL / RL with policy weight updates (deferred until APO plateaus)
- Multi-host distributed training (single-host n_runners only)
- Online learning from production traffic (training is offline-only)
- New MemPalace MCP tools (consume existing 28; don't add)
- `diary_write_policy` resource (deferred to v2; needs drawer-id provenance from `diary_ingest`)
- Vector backends other than ChromaDB
- Web UI (CLI only)
- Encryption-at-rest of scratch palaces
- Auto-pruning tunnels by utility score (Game D logs only; pruning is future work)
- $/token budget tracking beyond per-round metrics

---

## Critical files to modify (paths)

- `/Users/kennethchambers/Documents/GitHub/kent/agent/cli.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/builtin/__init__.py`
- `/Users/kennethchambers/Documents/GitHub/kent/pyproject.toml`

## Critical files to create (paths)

- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/__init__.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/rollout.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/palace_isolation.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/critic_scorer.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/swap_pair.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/apo_runner.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/recall_games.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/scope_eval.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/closet_fidelity.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/tunnel_utility.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/datasets.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/eval_harness.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/builtin/task_boundary.py`
- `/Users/kennethchambers/Documents/GitHub/kent/tests/training/test_rollout.py`
- `/Users/kennethchambers/Documents/GitHub/kent/tests/training/test_palace_isolation.py`
- `/Users/kennethchambers/Documents/GitHub/kent/tests/training/test_critic_scorer.py`
- `/Users/kennethchambers/Documents/GitHub/kent/tests/training/test_recall_games.py`
- `/Users/kennethchambers/Documents/GitHub/kent/tests/training/test_swap_pair.py`
