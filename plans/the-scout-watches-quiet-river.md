# Plan: Self-directed training suggestions (the scout)

## Context

**Why:** kent's training pipeline is implemented but *passive* — a human has to know which resource to optimize. We already collect rich behavioral signals: `wake_up.jsonl` (recall@k, scope accuracy, closet fidelity), `tunnel_utility.jsonl` (citation rate), live transcripts (`memory_recall*` calls + results), diaries. None of that feeds back into a "what should I train next?" decision.

**The user's ask:** kent should always be looking for training opportunities — during chat or via cron — and **prompt the user for approval** before kicking off training. No autonomy.

**Approach:** A **scout** module aggregates behavioral signals into ranked `ResourceRecommendation`s, persists them to a deduplicated suggestion store, and surfaces them at safe interaction points (REPL session start, `/suggest`, `kent train suggest`). Training only runs after explicit user `y`. We **also** track each accepted training run as a *pathway* through the memory graph so we can answer "which parts of the palace are causing repeated retraining."

**Training is proven to work end-to-end; what's unproven is that it compounds.** Per `README.md`:
- The training *signal* is verified live: `test_recall_metric_responds_to_query_quality` (green) shows drawer-aware queries score 0.323 vs 0.027 unrelated, 3/3 pairwise wins.
- The rollout path is verified live: `test_rollout_e2e_atlas` (green) — Qwen called `memory_recall`, critic scored 1.000.
- APO itself is verified to run end-to-end on a real LLM: `test_apo_train_query_rewrite_policy_atlas` (partial green) — round 01 completes, 4 rollouts at 9-13s each, the algorithm produces an edited candidate prompt. The shutdown hang is an upstream AgentOps issue, not a training failure. v0=0.866 → v1=0.778 in a single round is exploration, not regression.

What hasn't been demonstrated yet:
- **Multi-round improvement compounds** (only round 01 has ever completed; suggested test #5).
- **Optimized prompt beats seed at inference** (`test_retrieval_policy_ab_against_atlas` is wired but never executed; suggested test #6).

The scout is safe to ship today — `drift_gate` is the regression guard, and every suggestion requires explicit user `y`. But the value of accepting suggestions scales with how reliably APO improves prompts. Get tests #5 and #6 green soon (not as a hard prerequisite) so the scout's recommendations are worth accepting.

---

## Architecture

### Three input streams → one decision → one approval surface → one pathway log

```
INPUTS
  (1) Live signals (during chat)         ~/.kent/scout/signals.jsonl
  (2) Wake-up metrics (already produced) ~/.kent/lightning_store/metrics/{wake_up,tunnel_utility,drift,consensus}.jsonl
  (3) Transcripts (already produced)     ~/.cache/kent/transcripts/<session_id>.jsonl
                          ↓
DECISION
  scout.analyze(window_days=7) → list[ResourceRecommendation]
                          ↓
APPROVAL
  ~/.kent/scout/suggestions.jsonl (pending|accepted|rejected, append-only)
  Surfaces: REPL session-start banner, /suggest, kent train suggest, kent scout (cron)
                          ↓
PATHWAY (on accept)
  ~/.kent/scout/pathways.jsonl (one row per accepted training run)
```

### Signal taxonomy (v1 — 3 signals only)

| Signal kind | Detection | Maps to resource |
|---|---|---|
| `recall_miss` | `memory_recall*` returns the literal string `"(no memories found)"` | `query_rewrite_policy` |
| `recall_a1_low` | `recall_a1 < 0.4` rolling mean over last 5 wake_up rounds | `query_rewrite_policy` |
| `scope_accuracy_low` | `scope_accuracy < 0.5` rolling mean over last 5 rounds | `scope_policy` |

**Dropped (per `plans/quiet-bandit-on-laptop.md` Setup C):** `closet_fidelity` is observable but **not tunable** — closet summaries are generated inside `mempalace.sweeper`, kent has no prompt knob. Suggesting `closet_summary_policy` training is a dead-end. Stays as a `kent doctor` regression metric.

**Deferred to v2** (per review — too noisy or require API changes): `recall_unused` (stop-word false positives + needs structured tool output), `wing_scope_mismatch` (ambiguous coupling), `diary_fidelity_gap`, `tunnel_uncited`, `repeated_topic`.

**Why string proxy for `recall_miss`:** `MemoryRecall.call()` (`agent/builtin/memory_recall.py`) returns prose only — similarity scores aren't surfaced. A "<0.4 similarity" heuristic is unobservable without an API change. Use the literal `"(no memories found)"` from `MemPalaceStore.recall()` and defer score-based detection to v2.

### Dataclasses (in `scout.py`, no separate `signal_types.py`)

```python
@dataclass
class Signal:
    id: str                  # sha256(kind + ts + tool_call_id)[:16]
    kind: str                # one of the 4 signal kinds
    ts: float
    session_id: str | None
    wing: str | None
    query: str | None        # for recall-derived signals
    drawer_ids: list[str]    # populated when available; [] today (palace API gap)
    metric_value: float | None  # e.g. recall_a1 = 0.31

@dataclass
class ResourceRecommendation:
    resource: str            # one of apo_runner.RESOURCE_ORDER
    score: float             # severity × volume, 0..1
    rationale: str           # one sentence
    supporting_signal_ids: list[str]
    example_path: Path | None
    suggested_at: float
    status: str              # "pending" | "accepted" | "rejected"
    primary_critic_score: float | None
    suggestion_id: str       # sha256(resource + day)[:16] — used for dedup
```

Dropped from v1: `n_signals`, `window_days`, `consensus_critic_score` (dead provenance), `expired` / `accepted_drifted` statuses (use a separate pathway log).

### Scoring (simplified)

```
score = clamp(severity × volume, 0, 1)
  severity = (threshold − observed) / threshold       # how far below threshold
  volume   = log-scaled signal count                  # 1=0.3, 5=0.7, 20+=1.0
```

The original `confidence` multiplier is dropped: the proposed formula was inverted (no-consensus 0.7 > primary-only 0.5), and it relied on `eval_harness.consensus_check` which is `async def` and makes live LLM calls — incompatible with `kent scout` (cron, zero-token).

### Live signal collection

Wire a `SignalCollector` into `_stream_one_turn` in `agent/cli.py` (the only site that consumes `agent.loop.run` events — `_repl` and `cmd_run` both delegate there).

```python
# agent/cli.py:_stream_one_turn
async for ev in run(...):
    if collector is not None:
        collector.observe(ev)            # NEW
    # ...existing dispatch...
```

The collector:
- Watches `ToolResult` for `tool_call.name in {"memory_recall", "memory_recall_here"}`; captures `(query, result_text, active_wing)`.
- On `Terminal`, flushes a batch to `~/.kent/scout/signals.jsonl` (one fsync per turn-cycle).
- Default **off**. Opt in via `KENT_SCOUT_ENABLED=1`. No `--no-scout` flag, no per-entry-point default split.

### Suggestion store (inline in `scout.py`, ~40 LOC)

Append-only JSONL at `~/.kent/scout/suggestions.jsonl`. Locking via `fcntl.flock` (mirroring `agent/memory/diary.py:35-42`). **Status updates use logical-delete pattern, not in-place mutation:** to reject suggestion `s_abc`, append a new row `{suggestion_id: "s_abc", status: "rejected", ts: ...}` with the same `suggestion_id`. Reads collapse rows by `suggestion_id` (last-write-wins). Avoids the cron-vs-REPL mid-file race the review flagged.

Dedup key: `suggestion_id = sha256(resource + day)[:16]`. Re-running `kent scout` the same day with the same recommendation is a no-op.

### Pathway tracking (the new bit)

When a user accepts a suggestion and training completes, append one row to `~/.kent/scout/pathways.jsonl`:

```python
@dataclass
class TrainingPathway:
    pathway_id: str          # sha256(suggestion_id + train_run_id)[:16]
    created_at: float
    suggestion_id: str       # links to suggestions.jsonl
    resource: str
    signal_ids: list[str]    # links into signals.jsonl
    drawer_ids: list[str]    # union of all signal.drawer_ids — populated as palace API exposes them
    wing: str | None         # active wing when signals fired (modal value)
    queries: list[str]       # the failing queries that drove signals
    train_run_id: str        # APO run identifier from train_resource()
    pre_train_score: float | None
    post_train_score: float | None
    drift_detected: bool | None
    examples_dir: Path
```

**Why this matters:** over weeks, `pathways.jsonl` answers "which drawers / wings / query patterns are repeatedly triggering training?" — a maintenance signal that the *palace itself* may need pruning, splitting, or wing renaming, not the prompt. v1 captures the data; visualization (overlay on `kent viz`) is v2.

`drawer_ids` is an empty list today because `MemoryRecall.call()` doesn't surface drawer IDs in its `ToolResult.output`. The schema reserves the field; populating it is a follow-up that touches `agent/builtin/memory_recall.py` and `MemPalaceStore.recall()` to return structured results. Until then, pathway analysis works on `(wing, query)` tuples.

### CLI changes

`kent train` is currently a flat parser with required `--resource` (`agent/cli.py:1769-1819`). **Breaking change:** convert to a subcommand dispatcher.

```
kent train run --resource X --pair P …    # current behavior, --resource still required
kent train suggest                          # list pending suggestions (read-only)
kent train auto --yes --suggestion-id ID    # accept + train non-interactively
kent scout                                  # cron entrypoint: analyze, append, print 1-line summary
```

`--pair` selection (the review flagged this as unspecified): default is the active swap pair from `~/.kent/swap_pairs.toml` (or whatever `swap_pair.load_*` reads); `--pair NAME` overrides. If neither is configured, `kent train auto` exits 2 with a clear message.

Document the breaking rename in the commit message; old `kent train --resource X` invocations stop working.

### REPL slash commands (added inline to `_handle_slash` in `cli.py:500+`, no separate `scout_cli.py`)

```
/suggest                  # list pending suggestions, newest first
/suggest accept N         # shell into `kent train run --resource X --examples-dir Y --pair <active> --rounds 1 --runners 2`
/suggest reject N         # append rejected row
```

At REPL session start, if any `pending` suggestions exist, print one banner line:

```
[scout] 2 pending training suggestions — /suggest to review
```

No mid-chat prompts in v1. (The original "max one per session at safe boundaries with score≥0.7" was three dimensions of UX gating for a feature whose primary failure mode is users ignoring it — solve that first.)

### Reuse (do not modify)

- `agent.loop.run` — yields `ToolResult`, `AssistantMessageComplete`, `Terminal`. Collector observes; loop unchanged.
- `agent.training.apo_runner.RESOURCE_ORDER` — the only resource taxonomy. Scout never invents new resources.
- `agent.training.apo_runner.train_resource` — invoked by `kent train auto --yes` and `/suggest accept`. No API change.
- `agent.training.swap_pair.load_*` — pair resolution.
- `agent.training.eval_harness.drift_gate(val_reward, holdout_reward) -> dict` — returns `{"drift_detected": bool, ...}`. After training, scout reads `result["drift_detected"]` and writes it onto the pathway row.
- `~/.kent/lightning_store/metrics/consensus.jsonl` — read directly for confidence audit (NOT calling `consensus_check` live; that path makes LLM requests and breaks cron-mode zero-token guarantee).
- `agent/memory/diary.py:35-42` — `fcntl.flock` pattern to copy.

### Files to modify

- `/Users/kennethchambers/Documents/GitHub/kent/agent/cli.py`
  - Restructure `train` parser into subcommand dispatcher (`train run` keeps current behavior).
  - Add `train suggest`, `train auto`, top-level `scout` subcommands.
  - Add `/suggest`, `/suggest accept N`, `/suggest reject N` cases to `_handle_slash`.
  - REPL banner line at session start when pending suggestions exist.
  - Thread `SignalCollector` into `_stream_one_turn` (not `_repl` / `cmd_run`).

### Files to create

```
agent/training/
  scout.py                # dataclasses + analyze() + SuggestionStore + mine_examples() + record_pathway()
  signal_collector.py     # event observer; writes signals.jsonl

tests/training/
  test_scout.py           # scoring, dedup, status transitions (logical-delete), example mining, pathway recording
  test_signal_collector.py # ToolResult event → Signal extraction
```

Total: 2 new modules, 2 new test files, 1 modified file. Down from the original 6+5+1.

---

## Critical risks

1. **Reward hacking via self-selected training.** Signals derive only from observable failure (empty recall, low metric values). No actor self-reports. Drift gate flag is recorded on the pathway so resurfacing a drift-detected resource is suppressed for 30 days.

2. **Auto-mined examples are noisy.** For `query_rewrite_policy`: only emit examples where `Layer3.search_raw(paraphrase, wing=…)` hits the same drawer the failing query missed. **This requires an LLM rewriter** — so `mine_examples()` is *not* called from `kent scout` (cron, must stay token-free); it runs only from `/suggest accept N`. Document this clearly in the docstring; the cron path mines nothing.

3. **Signal log unbounded growth.** Defer rotation. At ~200 bytes/turn, 50MB ≈ 250k turns away.

4. **Cron + REPL race.** Logical-delete append-only pattern; reads collapse by `suggestion_id`. No mid-file mutation.

5. **APO costs real tokens.** No auto-yes path that bypasses approval. `kent train auto --yes` requires `--suggestion-id`; the slash-command path requires per-suggestion `accept`.

6. **Pathway log without drawer IDs is partial.** Until `MemoryRecall` surfaces drawer IDs, `pathways.jsonl` records `(wing, query)` only. This is enough for "which wing keeps causing retraining" analysis but not "which specific drawer." Schema is forward-compatible.

7. **Compounding-improvement is unproven.** APO is verified to *run* end-to-end (test_apo_train_query_rewrite_policy_atlas, partial green); compounding multi-round gain and trained-vs-seed inference A/B (suggested tests #5 and #6) are still TBD. `drift_gate` catches regressions per-run, but until #5/#6 are green the user should treat scout suggestions as "worth a token gamble" rather than "guaranteed wins."

8. **Upstream bug dependencies (per `plans/quiet-bandit-on-laptop.md`).** Two open bugs degrade scout output until fixed; scout still ships, but `kent doctor` should flag which fields to trust:
   - **Bug #1** (`val_reward` returns `-inf` rendered as `0.0` in `apo_runner.py:133`): pathway rows' `pre_train_score` / `post_train_score` are unreliable until the `_history_best_score or 0.0` truthiness bug is fixed.
   - **Bug #5** (wing-name leak in `scope_eval.py:106-110`): `scope_accuracy_low` signal is inflated by ground-truth leakage; rationales may overstate the gap.
   - **Bug #3** (consensus_check fed dummies): already mitigated — scout reads `consensus.jsonl` directly rather than calling `consensus_check`.

9. **APO laptop budget.** `quiet-bandit` documents that APO defaults (`gradient_batch_size=4`, `val_batch_size=16`, `beam_width=4`, `branch_factor=4`) produce 60-120 min/round on a laptop. Scout's accept handler always passes `--rounds 1 --runners 2` (and any other laptop knobs the user sets in `~/.kent/config.json`); it never invokes APO with stock defaults.

---

## Verification

### 1. Unit (offline, no LLM)
```
uv run pytest tests/training/test_scout.py tests/training/test_signal_collector.py -m "not integration"
```
Asserts:
- Signal extraction from synthetic event traces.
- Scoring math (`severity × volume`).
- Dedup by `suggestion_id`.
- Logical-delete: appending `rejected` after `pending` for same `suggestion_id` → reads return `rejected`.
- `mine_examples()` returns `None` when no paraphrase validates (avoids garbage data).
- Pathway row written with correct schema on accept-and-train.

### 2. Cron-mode smoke (zero LLM)
```
mkdir -p ~/.kent/lightning_store/metrics
echo '{"ts":1714400000,"round":1,"recall_a1":0.31,"scope_accuracy":0.7}' \
  > ~/.kent/lightning_store/metrics/wake_up.jsonl
uv run kent scout
```
Asserts: `~/.kent/scout/suggestions.jsonl` has one `pending` row with `resource=query_rewrite_policy`; stdout `scout: 1 new`; zero OpenAI requests.

### 3. Dedup
Run `kent scout` 100 times in a tight loop with the same fixture. Asserts: 1 row total.

### 4. REPL accept flow (manual smoke)
```
uv run kent
> /suggest
> /suggest accept 1   # only with a real swap pair configured
```
Asserts: shells into `kent train run --resource …`; on completion, `~/.kent/scout/pathways.jsonl` has one row linking the suggestion to the train run; `drift_detected` populated from `drift_gate` return dict.

### 5. Reward-hacking probe
Inject 5 confidently-wrong assistant turns with no `memory_recall` calls. Run `kent scout`. Asserts: zero recommendations (no observable failure → no signal).

### 6. CLI breaking change
```
uv run kent train --resource X   # old form, must error with clear message
uv run kent train run --resource X --pair P …   # new form, must work
```

### 7. Pathway analysis spot-check
After 3 accepted training runs across 2 wings, manually inspect `pathways.jsonl`. Asserts: each row links signals→suggestion→train run; `wing` field correctly identifies which wing dominated each pathway.

---

## Out of scope (v1)

- **Auto-training without approval.** `kent train auto` always requires `--yes` + `--suggestion-id`.
- **The 5 deferred signals** (`recall_unused`, `wing_scope_mismatch`, `diary_fidelity_gap`, `tunnel_uncited`, `repeated_topic`).
- **Confidence multiplier in scoring.** Drop until v2 once recommendations show real noise.
- **In-session mid-chat prompts.** REPL banner + `/suggest` only.
- **Status `expired` / `accepted_drifted`.** Use the pathway log for drift suppression.
- **Drawer-ID populated `signals` and `pathways`.** Requires `MemoryRecall` API change; reserved in schema.
- **Pathway visualization on `kent viz`.** Data captured; overlay deferred.
- **Cross-actor learning.** Pathways are per-(actor, critic) pair.
- **Learned scoring.** Heuristics only.
- **Web UI / dashboard.** CLI + REPL.

---

## Critical files to modify (paths)

- `/Users/kennethchambers/Documents/GitHub/kent/agent/cli.py`

## Critical files to create (paths)

- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/scout.py`
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/signal_collector.py`
- `/Users/kennethchambers/Documents/GitHub/kent/tests/training/test_scout.py`
- `/Users/kennethchambers/Documents/GitHub/kent/tests/training/test_signal_collector.py`
