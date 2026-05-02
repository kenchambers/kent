# Plan: Prop Risk Monitor — Cron Pipeline & Adaptive Framework

End-to-end blueprint for the **Prop Account Risk Monitor** cron: queries MT5
MCP, calculates Ernest Chan-style edge metrics across all funded prop accounts,
evaluates each focus magic against a framework-specific rule set (FTMO 2-Step
vs Goat Funded "Instant Pro"), and posts a structured Discord report.

This is the canonical reference for: (a) understanding what the cron does,
(b) extending or rewriting the pipeline, (c) reproducing the analysis
manually, (d) instructing an LLM on how to operate or modify it, and (e)
porting it to the kent agent.

---

## Context

A trader running 6 prop accounts (2 FTMO, 4 GoatFunded) needs continuous
monitoring across:

- Account-level state (balance, equity, drawdown vs broker rules)
- Per-magic strategy performance (Sharpe, expectancy, win rate, profit factor)
- Streak / regime-shift detection (consecutive losses with binomial p-value)
- Broker-specific rule fitness (FTMO 2-Step vs GFT Instant Pro have different
  kill conditions)

Historical pain point: humans alert on every red day. The point of this cron
is to **suppress noise** and only escalate when the math says something is
structurally wrong — Sharpe collapse, streak past 2σ, broker DD limit being
approached. It's an **emotional firewall**.

---

## 1. Cron Definition

| field | value |
|---|---|
| **ID** | `c80372b0` (replaced `6898bab7` on 2026-04-26) |
| **Name** | Prop Account Risk Monitor |
| **Schedule** | `0 7-21/2 * * 1-5` (every 2 hours, Mon–Fri, 7 AM – 9 PM) |
| **Timezone** | America/Chicago |
| **Model** | `sonnet` (Sonnet 4.6) for orchestration; per-magic gatekeeper escalates to `opus` |
| **Timeout** | 900 s |
| **Protected** | yes — do not auto-modify or delete |
| **Discord channel** | `1467304221362622555` |

The cron prompt is short — it just runs the analyzer script, formats the
output, and posts. All real logic lives in `scripts/prop_risk_*.py` and the
framework markdown prompts.

---

## 2. Architecture (4-layer; 7-stage pipeline)

```
                  ┌──────────────────────────────┐
                  │  1. DATA — MT5 MCP queries   │
                  │  (live broker state + deals) │
                  └──────────────┬───────────────┘
                                 ▼
                  ┌──────────────────────────────┐
                  │  2. STATS — Ernest Chan calc │
                  │  (deterministic Python)       │
                  │  Sharpe, expectancy, streak,  │
                  │  profit factor, broker breach │
                  └──────────────┬───────────────┘
                                 ▼
                  ┌──────────────────────────────┐
                  │  3. REFLECTION — rule-based  │
                  │  classifier (two-pass +      │
                  │  consensus). GREEN/YELLOW/RED │
                  └──────────────┬───────────────┘
                                 ▼
                  ┌──────────────────────────────┐
                  │  4. GATEKEEPER — LLM verdict │
                  │  (broker-specific prompt)     │
                  │  PASS / CONDITIONAL / FAIL    │
                  └──────────────────────────────┘
                                 ▼
                       Discord report + state
```

**Why the split:** layers 1–3 are **deterministic** (no LLM, no token cost,
fully reproducible). Layer 4 is the only LLM call and only fires on magics
that pass minimum-data thresholds. This keeps cost low and makes the
"is the strategy decaying?" answer cache-friendly.

The pipeline expands into **7 explicit stages**:

```
Stage 0 — Preflight     ──▶ focus magics + MT5 connectivity test
Stage 1 — Data pull     ──▶ MCP calls per account (info + history)
Stage 2 — Filter        ──▶ drop trades not matching focus magics
Stage 3 — Stats (Chan)  ──▶ Sharpe, expectancy, PF, streak, DD headroom
Stage 4 — Classification ──▶ GREEN/YELLOW/RED via deterministic rules
Stage 5 — Gatekeeper LLM ──▶ PASS/CONDITIONAL/FAIL via framework prompt
Stage 6 — Format & post ──▶ Discord-ready table + Honest Read paragraph
```

---

## 3. Pipeline Stages

### Stage 0 — Preflight (mandatory; halt on failure)

**0a. Load focus magic numbers — source of truth:**
```bash
cat /home/administrator/.openclaw/workspace/memory/prop_account_focus_magics.json
```
The JSON has six keys (one per active account); each has an `active_magic`
array listing the magic numbers under monitoring. `prop_risk_analyzer.py`
reads this file at startup and uses it for every filter downstream. **If
you change focus magics, this file is the only place to edit.**

Example structure:
```json
{
  "ftmo_10k":            { "active_magic": [3456799], "strategy_name": "Triforce standard" },
  "ftmo_100k":           { "active_magic": [25],      "strategy_name": "Magic 25" },
  "gft_100k_live":       { "active_magic": [22],      "strategy_name": "Magic 22" },
  "gft_100k_phase1_942": { "active_magic": [18],      "strategy_name": "Magic 18" },
  "gft_100k_phase1_743": { "active_magic": [27],      "strategy_name": "Magic 27" },
  "gft_100k_phase1_613": { "active_magic": [18, 98],  "strategy_name": "Magic 18 + Magic 98" }
}
```

**0b. Test MT5 MCP connectivity:**
```python
import sys
sys.path.insert(0, '/home/administrator/.openclaw/workspace')
from scripts.mt5_connectivity import test_live, write_status
if not test_live():
    write_status("outage", was_outage=False)
    raise RuntimeError("MT5 MCP unreachable — halt")
write_status("live", was_outage=last_was_outage)
```
The MCP server runs on the Windows host at `http://172.29.0.1:8000/mcp`
(WSL → Windows). If `test_live()` fails, the cron halts and posts an outage
report. It does **not** trust cached state.

### Stage 1 — Data pull (per account)

Two MCP calls per account, via `scripts/mt5_http_client.py`:

**Account snapshot:**
```bash
python3 scripts/mt5_http_client.py call get_mt5_account_info \
  '{"account_name":"<alias>"}'
```
Returns balance, equity, leverage, currency, margin, server.

**Trade history (canonical date window):**
```bash
python3 scripts/mt5_http_client.py history <alias> \
  2026-03-03 $(date -d "+1 day" +%Y-%m-%d)
```
- `date_from = 2026-03-03` is the post-MT5-renaming clean-data start point.
- `date_to = TOMORROW` because the API treats `date_to` as an **exclusive**
  upper bound — using `today` silently drops all intraday trades.

**Common errors that produce empty / incorrect results:**

| mistake | symptom | fix |
|---|---|---|
| Wrong tool name `get_position_history` | empty list | use `get_mt5_position_history` |
| `from_date` / `to_date` instead of `date_from` / `date_to` | tool error | use `date_from` / `date_to` |
| Missing `date_to` | tool error | tool requires both bounds |
| `date_to = today` | intraday trades dropped | use `tomorrow` (exclusive bound) |

### Stage 2 — Filter to focus magics

For each account, drop every deal whose `magic` field isn't in
`prop_account_focus_magics.json[account].active_magic`. Old / retired
strategies contaminate the sample if not filtered. The analyzer logs a
count of filtered vs kept deals so you can verify.

### Stage 3 — Statistical layer (Ernest Chan framework)

Computed in `scripts/prop_risk_stats.py` — pure Python + numpy/scipy, no LLM.
Each focus magic gets:

| Metric | Definition | Threshold reference |
|---|---|---|
| **n (trade count)** | Closed trades for this magic since `date_from` | n < 30 → no claims; n < 200 → preliminary only |
| **Sharpe ratio** | `mean(daily_returns) / std(daily_returns) × √252` | < -2.0 = RED; -1 to -2 = YELLOW; ≥ 0 = GREEN-eligible |
| **Win rate** | `wins / total` | combined with R:R below |
| **Expectancy** | `mean(per-trade P&L)` in $ | must clear commission drag |
| **Profit factor** | `gross_profit / abs(gross_loss)` | < 1.30 = variance-dominated; ≥ 1.50 = deployable |
| **Consecutive losses (streak)** | Count from most recent backwards | combined with binomial p-value below |
| **Streak probability** | `loss_rate ^ streak` (binomial) | p < 0.05 + n ≥ 200 → 2σ event |
| **DD headroom** | `(broker_limit_threshold − equity)` | < 20% of limit = YELLOW |
| **MAE p95** | 95th-percentile MAE % across losing trades | tail-risk profile; feeds gatekeeper math gate |

**Hard rules (from `CLAUDE.md` and operator policy):**

- **n < 30** → INSUFFICIENT_DATA. Period. No claims.
- **n < 200** → preliminary findings only, do not promote/demote.
- **n ≥ 200** → claims allowed at p < 0.05.
- **Sharpe < -10** → structural failure (not market variance), kill fast.
- **Hard broker DD limits** are the only non-statistical kill switches.
- **Per-symbol slices of n < 30** are descriptive, not prescriptive — do not recommend cuts.

### Stage 4 — Deterministic classification (two-pass reflection)

`scripts/prop_risk_reflection.py` runs a **two-pass** classifier:

**Pass 1** — apply explicit rules:
```
IF broker_limit_breached       → RED
IF n_trades < 30                → YELLOW (insufficient_data)
IF sharpe < 0 AND streak_p<0.05 → RED (decay + variance)
IF sharpe > 3 AND streak_p<0.05 → YELLOW (variance only, not decay)
IF sharpe < baseline × 0.6      → RED (40% decay from established baseline)
ELSE                             → GREEN
```

**Pass 2 (Reflection)** — re-checks raw data, confirms or corrects Pass 1.

**Consensus** — if passes disagree, **use the stricter** classification.
Never auto-promote, always auto-demote.

This stage runs deterministically (no LLM), so the same input always
produces the same status — important for cron reliability and audit.

### Stage 5 — Adaptive framework gatekeeper (LLM)

`scripts/prop_risk_gatekeeper.py` selects one of two framework prompts based
on the account, then spawns a **subscription-tier** Claude Code subagent to
return a structured `PASS / CONDITIONAL / FAIL` verdict. Frameworks differ
because the prop firm rules differ — a strategy that survives Goat Funded's
2% floating per-trade kill doesn't necessarily handle FTMO's 5% daily DD
anchored at midnight CET.

**Framework selection (auto-route by account name):**
```python
INSTANT_PRO.scope_accounts = {
    "gft_100k_live", "gft_100k_phase1_942",
    "gft_100k_phase1_743", "gft_100k_phase1_613",
}
FTMO_2STEP.scope_accounts = {"ftmo_10k", "ftmo_100k"}
```

**Per-magic invocation (per account, per focus magic):**

1. `_summarize_magic` builds a stats blob (Sharpe, WR, R:R, MAE, expectancy,
   sample size, etc.) including 95th-percentile MAE pulled from the
   `calculate_excursion_analysis` MCP tool.
2. `_format_user_message` renders the stats blob plus the framework's
   `closing_note` (which tells the LLM what rule set is active and what
   bottleneck phase to look for on FTMO).
3. `subprocess.run([CLAUDE_BIN, "-p", "--model", "opus"], input=..., env={...system_prompt...})`
   invokes Claude Code in headless mode. The system prompt is the framework
   markdown loaded from disk.
4. `_extract_verdict` parses the response for `VERDICT: PASS|CONDITIONAL|FAIL`.
5. The verdict + raw response are stored in `memory/prop_risk_state.json`.

**Critical operational rule** (per `CLAUDE.md`): the gatekeeper MUST use
the Claude subscription (`claude -p --model opus`), NOT the API. Crons
are subscription-only.

**Why opus:** the gatekeeper does multi-step math against a complex rule
set. Sonnet has been observed to round numbers up to make a strategy
qualify; Opus does not. This is one of the few places where model choice
materially affects output quality.

**Why a separate LLM stage at all?** The Chan layer answers "is the data
showing decay?" The gatekeeper answers "even if the metrics look fine,
will this strategy survive the prop firm's specific rule set?" Those are
different questions — e.g., a strategy with Sharpe +1.2 but a
95th-percentile MAE of 3.2% will fail Goat Funded's 2% floating rule.
The Chan layer would call it GREEN; the gatekeeper calls it FAIL.

#### Framework details

**INSTANT_PRO (Goat Funded "Instant Funding PRO"):**

- 4% trailing total DD on equity
- **2% FLOATING per-trade kill** (mark-to-market, not realized) — the killer
- 20% consistency rule (no single day > 20% of cycle profits)
- 5 min valid trading days (≥ +0.5% balance to count)
- 14-day payout cycle; DD resets after each payout
- Min thresholds: expectancy ≥ 0.20R, PF ≥ 1.30, WR ≥ 55%, risk ≤ 1%,
  95th pct MAE ≤ 2.67R, n ≥ 150 OOS, backtest ≥ 6 months,
  max historical DD < 3.5%

**FTMO_2STEP (FTMO Challenge → Verification → Funded):**

- Three-phase rule set evaluated together; **flag the bottleneck phase**
- Phase 1: +10% target, 5% daily DD anchored at midnight CET (incl. floating),
  10% total DD, 4 min trading days
- Phase 2: +5% target, same DD rules
- Funded: trailing 10% floor (only rises with EOD highs), 5% daily DD,
  80% profit split scaling to 90%
- No per-trade floating cap, no consistency rule — wider strategies allowed
- Min thresholds: expectancy ≥ 0.15R, PF ≥ 1.30, WR ≥ 45%, risk ≤ 2% Phase 1
  / 1% Funded, max single-day DD < 3.5%, n ≥ 150 OOS

The framework prompts (`cron_instructions/instant_pro_gatekeeper.md` and
`ftmo_2step_gatekeeper.md`) include full math derivations, win-rate × R:R
matrices, and a four-test gate (MAE check, consistency / streak survival,
bootstrap confidence, paper trial). The gatekeeper LLM works through these
in order and stops at the first failure.

#### Excursion data dependency

`_fetch_mae_for_losing_trades` calls the `calculate_excursion_analysis` MCP
tool with `winning_trades=False, losing_trades=True, timeframe="M1"` and
filters to the focus magic via the `strategy` field. If the tool returns
sparse or empty data, open the symbol's chart in MT5 to force tick download
and re-run. Without MAE data the four-test gate cannot complete; the
gatekeeper falls back to math-layer-only and flags the gap explicitly.

### Stage 6 — Format & post

The cron prompt produces a Discord-ready markdown summary.

**Header:** `Prop Risk Monitor — YYYY-MM-DD HH:MM CDT`

**6-row table:**
```
| Account | Status | Equity | Focus n | Sharpe | WR | Flag |
|---------|--------|--------|---------|--------|----|----|
| ...     | 🟢/🟡/🔴 | $X,XXX | n=N | S | WR% | (gatekeeper verdict) |
```

**Gatekeeper line:** `TOO_EARLY: <count>` for magics still under n=200.

**Honest Read paragraph:** one paragraph in calm, peer-to-peer voice. Cite
the n<200 caveat where it applies. Numbers over adjectives. No emoji
padding. No performative helpfulness.

**Critical suppression rule:** if everything is GREEN with no flags AND
every focus magic has n ≥ 200, send `NO_REPLY` (suppress the message).
This is the **emotional firewall** rule — don't notify on normal variance.

**On any failure** (analyzer crash, MCP outage, parse error): post
`Prop Monitor error: <msg>` to the same channel.

---

## 4. Status Thresholds Reference

**🔴 RED triggers:**
- `Sharpe < -2.0` (structural failure territory)
- Broker DD limit breached (FTMO: 10% total / 5% daily; GFT: 8% total / 4% daily)
- Consecutive losses with `p < 0.05` AND `n ≥ 200`
- Gatekeeper verdict = `FAIL`

**🟡 YELLOW triggers:**
- `Sharpe -1.0 to -2.0` (degrading but not failed)
- DD headroom < 20% of broker limit
- Consecutive losses significant but `n < 200`
- Gatekeeper verdict = `CONDITIONAL`
- `n < 30` for any magic (INSUFFICIENT_DATA)

**🟢 GREEN:**
- `Sharpe ≥ 0` or insufficient data
- No broker-limit concerns
- Gatekeeper verdict = `PASS`

---

## 5. Source Files (in the OpenClaw workspace)

| File | Purpose |
|---|---|
| `cron_instructions/claude_code_crons.json` | Cron registry (id `c80372b0`) |
| `cron_instructions/prop_risk_sentinel.md` | Canonical operator instructions for the cron |
| `cron_instructions/prop_risk_monitor.md` | Earlier instructions doc — mostly superseded |
| `cron_instructions/instant_pro_gatekeeper.md` | INSTANT_PRO framework system prompt (Goat Funded) |
| `cron_instructions/ftmo_2step_gatekeeper.md` | FTMO_2STEP framework system prompt (FTMO Challenge / Funded) |
| `memory/prop_account_focus_magics.json` | Source of truth for which magics to monitor |
| `memory/prop_risk_state.json` | Per-run output state (status + verdicts per magic) |
| `memory/prop_risk_sentinel_log.jsonl` | Append-only run log for retrospective analysis |
| `memory/prop/<account>.md` | Long-term magic-level memory per account |
| `scripts/prop_risk_analyzer.py` | Top-level orchestrator — pulls data, calls stats, calls gatekeeper |
| `scripts/prop_risk_stats.py` | Ernest Chan calculations (Sharpe, streak prob, DD detection) |
| `scripts/prop_risk_reflection.py` | Deterministic GREEN/YELLOW/RED classification |
| `scripts/prop_risk_gatekeeper.py` | Adaptive framework selector + LLM invocation |
| `scripts/mt5_http_client.py` | MCP HTTP client (calls FastMCP server on Windows) |
| `scripts/mt5_connectivity.py` | Connectivity probe + write_status helper |

---

## 6. Operating It Manually

To reproduce the cron's analysis on demand:

```bash
cd /home/administrator/.openclaw/workspace
timeout 540 python3 scripts/prop_risk_analyzer.py
# → writes memory/prop_risk_state.json + prints the Discord-ready report
```

Per-magic gatekeeper run (dev / debug):
```bash
python3 scripts/prop_risk_gatekeeper.py --account ftmo_10k --magic 3456799
# Auto-selects framework based on account (FTMO_2STEP vs INSTANT_PRO)
# → prints stats blob + Claude Code's verdict
```

Force a framework override (only valid for testing, not for live posting):
```bash
python3 scripts/prop_risk_gatekeeper.py --account gft_100k_live --magic 22 \
    --framework ftmo_2step
```

---

## 7. State & De-duplication

- **Per-run state:** `memory/prop_risk_state.json` — one entry per
  `(account, magic)` with last status + timestamp. Used to detect status
  transitions.
- **Sentinel log:** `memory/prop_risk_sentinel_log.jsonl` — append-only
  log of every classification, useful for retrospective analysis.
- **Long-term memory:** `memory/prop/<account>.md` — human-readable
  timeline per account.

**De-duplication rule:** only re-alert if status **changed** OR **4+ hours**
since last RED alert for the same magic.

---

## 8. Failure Modes & Recovery

| Symptom | Likely cause | Recovery |
|---|---|---|
| Cron posts "MT5 MCP unreachable" | Windows host MCP server down or firewall closed | Restart `run_http_server.bat` on Windows; verify `configure_firewall.bat` ran |
| Account shows `n=0 trades` | `date_to=today` (exclusive bound) OR wrong tool name | Use `date_to=$(date -d '+1 day' +%Y-%m-%d)` and `get_mt5_position_history` |
| Sharpe values look wildly off | History contaminated with non-focus magics | Verify `prop_account_focus_magics.json` matches deployed EAs; filter is applied per-magic |
| Gatekeeper returns no verdict | LLM response missing the `VERDICT: ...` line | Inspect `memory/prop_risk_state.json` raw response field; framework prompt may need clarifying |
| `INSUFFICIENT_DATA` after rotation | Magic recently rotated; n < 200 trades since deploy | Wait until n ≥ 200; this is correct behavior, not a bug |
| Cron fires but Discord silent | All GREEN with all n ≥ 200 → intentional `NO_REPLY` | Check `memory/prop_risk_state.json` to confirm run succeeded |
| Excursion (MAE) data sparse | MT5 tick cache missing for the symbol | Open the symbol's chart in MT5 to force tick download; re-run |
| Single-account fetch fails mid-run | Transient network or terminal lockout | Account marked UNAVAILABLE for that run; other accounts continue |
| All accounts fail | MT5 MCP outage | Output `⚠️ MONITOR FAILED — MT5 MCP unavailable`; do not write state |

---

## 9. Broker-Rule Reference

| Broker | Max Total DD | Max Daily DD | Floating SL kill | Consistency rule |
|---|---|---|---|---|
| FTMO 2-Step | 10% (`$1k` on 10k / `$10k` on 100k) | 5% (anchored midnight CET, incl. floating) | none | none |
| GFT Instant Pro | 4% trailing on equity | none | **2% floating per-trade cap (mark-to-market)** | day > 20% of cycle profits = payout denied |

---

## 10. Extension Notes

If you want to extend this pipeline:

- **Add a new prop firm:** create a new framework file in `cron_instructions/`
  (use `instant_pro_gatekeeper.md` as a template), add a new `Framework`
  dataclass instance in `prop_risk_gatekeeper.py`, populate `scope_accounts`,
  and update the dispatcher near `evaluate_magic`.
- **Add a new metric:** add the calculation to `prop_risk_stats.py` and
  thread it through `_summarize_magic` so the gatekeeper's user message
  includes it. Update the framework markdown's `<mathematical_thresholds>`
  section to reference the new metric.
- **Add a new account:** edit `mt5_accounts.json` (server-side) AND add a
  matching entry to `memory/prop_account_focus_magics.json` (the source
  of truth for monitoring). Both files must agree.
- **Change the cron schedule:** edit `cron_instructions/claude_code_crons.json`
  for the `c80372b0` entry, then re-register via `CronCreate` with
  `durable: true`.
- **Tune thresholds:** the GREEN/YELLOW/RED rules in
  `prop_risk_reflection.py` are version-controlled and intentionally
  conservative. Changes here affect every account simultaneously. Prefer
  tuning the framework markdown (LLM-side) for account-class-specific changes.

---

## 11. Port-to-Kent Checklist

What kent needs to add to run this pipeline natively:

1. **Tool: `prop_risk_pull`** — wraps the MT5 MCP calls in Stage 1. Takes
   account list, returns structured `{account: {info, deals}}` dict.
2. **Tool: `prop_risk_stats`** — wraps the Ernest Chan calcs in Stage 3.
   Pure Python, no LLM, no MCP.
3. **Tool: `prop_risk_classify`** — wraps the two-pass reflection in Stage 4.
4. **Tool: `prop_risk_gatekeeper`** — spawns the Opus subagent with the
   broker-specific system prompt. Returns the verdict dict.
5. **Cron entry**: every 2h Mon–Fri 7–21 local, runs the orchestrator.
6. **State files** under `~/.kent/prop_risk/` mirroring the OpenClaw layout.

What kent should NOT do:

- **Do not embed broker passwords.** All credentials live in
  `mt5_accounts.json` on the Windows MCP server — kent only references
  account names.
- **Do not call `discord_send` from inside the cron handler.** Output via
  return-value / stdout; the cron system handles delivery. Calling
  `message`/`send` tools manually causes double-delivery.
- **Do not use the API for the gatekeeper.** Subscription-only via
  `claude -p --model opus` — see `CLAUDE.md` for the standing rule.
- **Do not run during off-hours.** Wastes MCP and gatekeeper budget; no
  new trade data to evaluate.

---

## 12. Design Philosophy

The pipeline is intentionally **layered and asymmetric**:

1. **Cheap checks first.** Connectivity → focus filter → deterministic
   stats run with no LLM call. If anything trips here, no LLM tokens spent.
2. **Statistical layer is uniform.** Every magic, every account, runs
   through the same Chan calculations. Output is comparable across accounts.
3. **Gatekeeper is adaptive.** Same stats produce different verdicts under
   different prop firm rule sets. This is the only place where account-type
   matters.
4. **Output is suppressible.** The cron is "an emotional firewall" — its
   job is to NOT notify when nothing has changed. `NO_REPLY` on all-GREEN
   with full samples is the desired path.
5. **State is durable.** `memory/prop_risk_state.json` is the single source
   for "what did the last run say?" — readable by other crons (e.g., a
   daily summarizer or a Discord-on-demand status responder).

The phrase **"diagnose with math, not coach"** appears verbatim in both
framework prompts. That tone discipline is the point — operators making
real-money decisions don't need encouragement; they need numbers and a
verdict.

**Mathematical philosophy:**

- `n < 30` → no claims. Period.
- `Sharpe < -10` → structural failure, not variance.
- `P < 0.05` → 2σ rare event, worth investigating.
- Hard broker limits are the only non-statistical kill switches.
- Pass-1 + Pass-2 disagreement always resolves to **stricter**.
- The gatekeeper's job is to find the **bottleneck phase** (FTMO) or the
  **structural rule the strategy violates** (GFT), not to coach.

---

## 13. Open Questions for v2

- **Adaptive thresholds:** should `Sharpe < -2.0` shift based on the
  strategy's own historical baseline? Currently absolute.
- **Per-magic baseline tracking:** store rolling 30-day Sharpe and compare
  to spot 40%+ decay (already in reflection rules, but not all magics have
  enough history).
- **Consistency-rule pre-warning:** GFT's 20% rule kills payouts silently —
  add a daily-cycle check that warns when one day's P&L exceeds 15% of
  cycle total.
- **Macro-event awareness:** integrate with Hydra signals so the sentinel
  can suppress alerts during known-volatile windows (FOMC, NFP, ECB).
- **Auto-pause:** when a magic goes RED, write a
  `halt_<account>_<magic>.flag` that the EAs read on init — close the
  loop without operator intervention.
- **Per-account cycle tracking** for GFT (14-day payout cycles): the
  cron currently treats time as monotonic; for GFT-side analytics it
  should reset stats at each payout boundary.
