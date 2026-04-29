# Plan: Lightweight training, the Agent Lightning audit, and what to fix

> Audit of kent's `agentlightning>=0.3` usage; a list of real bugs to fix before
> any more training experiments; and three lightweight training setups that
> finish in minutes on a laptop and produce measurable improvements.

## Context

kent currently runs Agent Lightning's APO loop end-to-end against Atlas Cloud
Qwen. The framework primitives are wired correctly, but the **defaults don't
fit a laptop budget** and the **reward signal doesn't always match what APO's
textual-gradient algorithm expects**. The recently-added drift gate /
consensus check helpers (`agent/training/eval_harness.py`) are mostly sound,
but one of them is fed dummy data by the caller in `cmd_train`. This doc
captures (1) the audit findings, (2) bugs that must be fixed before any
metric we log is trustworthy, and (3) two lightweight training paths that
beat APO on a laptop.

Alignment with upstream Agent Lightning: **3.5 / 5**. The wiring is canonical;
the *configuration* and *semantic fit* are where we drift.

## Bugs to fix first (every reported metric is suspect until these land)

| # | File:line | Symptom | Fix |
|---|---|---|---|
| 1 | `agent/training/apo_runner.py:133` | `val_reward` is broken: `getattr(algorithm, "_history_best_score", 0.0) or 0.0` — `_history_best_score` initializes to `float("-inf")`, and `-inf or 0.0` returns `-inf` (truthy). Every reported `val_reward` may be `-inf` rendered as a float. | `score = getattr(algorithm, "_history_best_score", float("-inf")); val_reward = float(score) if score != float("-inf") else 0.0` |
| 2 | `agent/training/rollout.py:134` | `max_turns=config.max_rounds` — `max_rounds` is the outer APO beam-rounds count (default 20), not the per-rollout LLM turn cap. `kent train --rounds 5` accidentally caps actor turns to 5. | Add `max_actor_turns: int = 8` to `TrainingConfig`; use that as `max_turns`. |
| 3 | `agent/cli.py:1116-1122` | `consensus_check` is fed five identical `[user, "ok."]` conversations → degenerate Spearman ranks → meaningless. Costs 10 critic LLM calls/training pair and tells you nothing. | Pass real transcripts from `_run_rollout_impl`'s `collected` field. |
| 4 | `agent/training/palace_isolation.py:75-77` | `assert_isolation` only checks the *first* diary file (`break` at L77). Hardlinks in later diaries slip through. | Iterate the full `diaries/` tree. |
| 5 | `agent/training/scope_eval.py:106-110` | Game C leaks ground truth — wing names in the prompt let the critic match labels semantically. Inflated wing-routing scores. | Anonymize wings as `wing_a`/`wing_b` in the critic prompt; map back after scoring. |
| 6 | `agent/training/tunnel_utility.py:84` | Substring match `"memory_recall" in content` misfires on natural-language messages ("I'll do a memory_recall."). | Inspect `tool_calls`, not message content. |
| 7 | `agent/training/rollout.py:140` | Critic LLM constructed per-rollout, never closed. Httpx connection leak under heavy training. | Cache one critic LLM per `TrainingConfig`; close on rollout completion. |

## Per-primitive correctness check (Agent Lightning ↔ kent)

| Primitive | Status | Notes |
|---|---|---|
| `PromptTemplate(engine="f-string")` | ✅ correct | The `poml` engine is for APO's *internal* gradient/edit prompts, not user templates — we never need to author POML. |
| `Trainer + APO + SharedMemoryExecutionStrategy + TraceToMessages` | ✅ canonical | Matches upstream `examples/apo/room_selector_apo.py`. The `SharedMemoryExecutionStrategy` choice for macOS spawn-pickling is the right workaround. |
| `@agl.rollout(task, prompt_template, rollout) → float` | ✅ correct | `agent/training/rollout.py:188-190`. |
| `agl.emit_reward(...)` + `return float` | ⚠ redundant | Both are accepted by the trace adapter; doubles the reward span emission. Harmless but noisy. |
| Sequential resource freezing (concat 5 prompt blocks) | ⚠ kent invention | Works for prompts that compose, but APO can only *edit* one slice while *seeing* the whole concatenation — fragile interactions. No version pinning of frozen resources, no detection of mid-train overwrites. |
| Default batch sizes | ❌ wrong for laptop | `gradient_batch_size=4`, `val_batch_size=16`, `beam_width=4`, `branch_factor=4` → ~128 rollouts/round × 30-60s/rollout = **60-120 min/round**. Drop everything to 1-2 for laptop usage; `test_apo_train_query_rewrite_policy_atlas` already discovered this empirically. |
| Reward range | ✅ correct | APO is monotone (higher better, no fixed range); kent's [0, 1] clamp is fine. |

## Verdict on the recent `eval_harness.py` additions

- **`drift_gate` — sound.** `|val − holdout| > 0.10` is the right shape for catching critic-overfit. Improvement: log *direction* — `val ≫ holdout` is the overfit signal; the inverse usually means the critic itself is noisy.
- **`evaluate_holdout` — sound but expensive.** ~1 min per holdout example. Acceptable.
- **`consensus_check` — implementation right, caller wrong.** See bug #3 above. The function itself is fine; `cmd_train` feeds it garbage.

## Lightweight training that actually works (laptop, minutes, useful deltas)

The user is correct that APO is overweight. APO costs **2-3× per-rollout** on
top of the actor (gradient + edit LLMs), and converges unreliably on
weaker open-source models — Microsoft's POML gradient templates are designed
for GPT-4-class reasoning. On a laptop with Atlas Cloud Qwen, the math
doesn't work for most resources.

### Setup A — Best-of-N for `query_rewrite_policy` (~30 min, no APO)

- **Signal:** recall@5 against a stratified diary fixture (50 drawers).
- **What changes:** the system prompt for `query_rewrite_policy`.
- **Method:** Hand-seed 8 candidate prompts (variations + 2 LLM-paraphrased). For each, run all 50 drawers through `agent/training/recall_games.py` (one LLM call + one vector search per drawer = ~5s each). Pick the top scorer. No beam search, no gradient, no critic.
- **Wallclock:** 8 × 50 × ~5s ≈ **30 min, single round**.
- **Verification:** 80/20 split on the drawer fixture; require the chosen prompt to beat baseline by ≥0.05 recall@5 *on the held-out 20*. The `drift_gate` helper is exactly the right shape here.

### Setup B — Few-shot pool for `memory_recall` tool description (minutes/round)

- **Signal:** "did the actor call `memory_recall` when it should have, with a sensible query?" Curate 30 fixture conversations (15 should-recall, 15 should-not). Score = binary tool-fired-correctly + 0/1 query-quality from a one-shot critic.
- **What changes:** the **few-shot examples** included in the system prompt for the `memory_recall` tool description (NOT the policy text).
- **Method:** Maintain a 12-candidate pool. Greedy add/remove one example per round; accept if held-out score increases.
- **Wallclock:** 30 × ~5s × 12 ≈ **30 min/round**, 3 rounds total.
- **Verification:** AUROC on a held-out 30-fixture set + drift_gate.

### Setup C (anti-recommendation) — Drop Game C as a training resource

`agent/training/closet_fidelity.py` measures something useful, but kent has
no knob to tune — closet summaries are generated inside `mempalace.sweeper`.
**Keep it as a `kent doctor` regression metric, not an APO resource.** Spend
the saved engineering on Setup A.

### Universal lightweight discipline (applies to every APO usage)

- `gradient_batch_size`, `val_batch_size`, `beam_width`, `branch_factor` → drop to 1-2.
- `run_initial_validation=False` once you trust the seed prompt.
- Always 60/20/20 split with the drift gate. Never trust val_reward alone.

## What we're explicitly NOT doing

- More APO experiments on `actor_system_prompt` or `retrieval_policy` until Setups A/B prove out.
- Tuning closet summaries (we don't own the prompt).
- Adding more games (D, E, F) until D's substring bug is fixed and the existing four prove they detect real regressions.
- Changing the Agent Lightning version pin until the bugs above are fixed against the current pin.

## Recommended next-session scope

One session, one goal:

1. **Fix the seven bugs** in the table above (tests required for #1, #3, #4, #6).
2. **Add `kent train --light A`** that runs Setup A end-to-end (no APO, no critic LLM, no Lightning machinery). Output: `~/.kent/lightning_store/light/query_rewrite_policy.txt` + drift report.
3. **Wire the consensus_check fix to use real transcripts** (one-line change in `cmd_train` — pass `_run_rollout_impl`'s `collected` instead of synthetic conversations).

Defer: Setup B (next session), broader APO retuning (only after A proves out).

## Files referenced (absolute paths)

- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/apo_runner.py` — bug #1
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/rollout.py` — bug #2, #7
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/eval_harness.py` — drift_gate, evaluate_holdout, consensus_check
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/scope_eval.py` — bug #5
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/tunnel_utility.py` — bug #6
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/palace_isolation.py` — bug #4
- `/Users/kennethchambers/Documents/GitHub/kent/agent/training/recall_games.py` — Setup A reuses this
- `/Users/kennethchambers/Documents/GitHub/kent/agent/cli.py:1116-1122` — bug #3
- `/Users/kennethchambers/Documents/GitHub/kent/tests/training/test_apo_e2e.py` — canonical wiring + the comment about GPT-4-class POML expectation
