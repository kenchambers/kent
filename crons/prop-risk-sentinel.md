# Plan: Prop Risk Sentinel Cron

A periodic prop-account health monitor. Pulls trade data from the MT5 MCP
server, runs Ernest Chan-style statistical evaluation in deterministic
Python, then escalates to a broker-specific LLM "gatekeeper" for higher-level
strategy verdict. Acts as an **emotional firewall** — only notifies on
statistically significant decay, not normal variance.

---

## Context

A trader running 6 prop accounts (2 FTMO, 4 GoatFunded) needs continuous
monitoring across:

- Account-level state (balance, equity, drawdown vs broker rules)
- Per-magic strategy performance (Sharpe, expectancy, win rate, profit factor)
- Streak / regime-shift detection (consecutive losses with binomial p-value)
- Broker-specific rule fitness (FTMO 2-Step vs GFT Instant Pro have different
  kill conditions)

Historical pain point: humans alert on every red day. The point of the
sentinel is to **suppress noise** and only escalate when the math says
something is structurally wrong — Sharpe collapse, streak past 2σ,
broker DD limit being approached.

Existing implementation lives in OpenClaw at:

- `cron_instructions/prop_risk_monitor.md` — cron prompt
- `scripts/prop_risk_analyzer.py` — main orchestrator
- `scripts/prop_risk_stats.py` — Ernest Chan calcs
- `scripts/prop_risk_reflection.py` — two-pass classifier
- `scripts/prop_risk_gatekeeper.py` — LLM-based broker-specific evaluator
- `cron_instructions/ftmo_2step_gatekeeper.md` — FTMO system prompt
- `cron_instructions/instant_pro_gatekeeper.md` — GFT system prompt

This document is the **kent-side blueprint** for porting that pipeline so
kent can run the sentinel itself (via heartbeat or a dedicated cron/cmd).

---

## Architecture (4 layers)

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
                  │  consensus). Output: GREEN /  │
                  │  YELLOW / RED                 │
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

---

## Schedule

- **Trading hours:** every 2 hours, Mon–Fri, 8 AM – 11 PM operator-local
- **Off-hours:** skip (no broker activity)
- **One-shot diagnostic:** runnable on demand (`kent /sentinel` or shell)

Skip the run entirely if:

- All accounts are outside trading windows for their broker
- MT5 MCP server is unreachable after 3 retries
- The previous run is still in flight (lock file)

---

## Process (step-by-step)

### Step 1 — Load focus-magic config

Source of truth: `memory/prop_account_focus_magics.json`. Each account maps
to a list of `active_magic` numbers. **Discard all trades with other magic
numbers** — they're old/expired strategies and pollute the stats.

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

### Step 2 — Pull MT5 data per account

For each account in the focus map, hit the MT5 MCP server via
`scripts/mt5_http_client.py` (see `docs/MT5_MCP_SETUP.md` for setup):

```bash
# Account info — balance, equity, margin, leverage, profit
python3 mt5_http_client.py call get_mt5_account_info \
  '{"account_name":"<acct>"}'

# Trade history — date_from clamped to clean-data start, date_to = today
python3 mt5_http_client.py call get_mt5_position_history \
  '{"account_name":"<acct>",
    "date_from":"2026-03-03",
    "date_to":"2026-12-31",
    "include_strategy_stats":true}'
```

**Common failure modes:**

- Wrong tool name: must be `get_mt5_position_history`, NOT `get_position_history`
- Wrong param names: must be `date_from`/`date_to`, NOT `from_date`/`to_date`
- Missing `date_to`: tool requires both dates

Filter the history to ONLY the focus magic numbers — drop everything else.

### Step 3 — Calculate metrics per magic (Ernest Chan layer)

For each `(account, magic)` pair with trades:

| metric | calculation | meaning |
|---|---|---|
| `n` | count of trades | sample size for statistical claims |
| `Sharpe` | `mean(returns) / std(returns) × √252` | annualized risk-adjusted return |
| `win_rate` | `wins / n` | hit rate |
| `expectancy` | `mean(P&L per trade)` in dollars | $ per trade on average |
| `profit_factor` | `gross_profit / gross_loss` | >1 = positive expectancy |
| `consecutive_losses` | streak from end of trade list | current losing streak length |
| `streak_p` | binomial P(streak ≥ k \| win_rate) | rarity of current streak; P < 0.05 = 2σ event |
| `dd_headroom` | `broker_max_dd - current_dd` | $ buffer before broker kills account |
| `mae_pct_95` | 95th percentile of MAE % across losing trades | tail-risk profile |

**Sample-size rules (Ernest Chan):**

- `n < 30` → INSUFFICIENT_DATA, no statistical claims
- `n < 200` → preliminary findings only, do not promote/demote
- `n ≥ 200` → claims allowed at p < 0.05

**Hard kill thresholds (no LLM, no debate):**

- `Sharpe < -10` → structural failure (not market variance)
- `broker_limit_breached` → immediate RED, escalate to operator
- `daily_dd > broker_daily_max × 0.8` → preemptive RED before breach

### Step 4 — Reflection-based classification

`prop_risk_reflection.py` runs a **two-pass** classifier:

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
This is the deterministic anti-bias layer — never auto-promote, always
auto-demote.

### Step 5 — Adaptive Gatekeeper (broker-specific LLM verdict)

For each magic that has `n ≥ 30` and is not already RED, run the
broker-specific gatekeeper. This is the only LLM call in the pipeline —
it produces a `PASS / CONDITIONAL / FAIL` verdict against the broker's
specific rule set.

**Two frameworks; auto-route by account name:**

| Framework | Scope accounts | System prompt | Why different |
|---|---|---|---|
| **`FTMO_2STEP`** | `ftmo_10k`, `ftmo_100k` | `cron_instructions/ftmo_2step_gatekeeper.md` | 5% daily DD anchored to midnight CET balance, 10% total DD, no floating per-trade kill, no consistency rule. **Three sequential phases** (Challenge → Verification → Funded), each with different optimal sizing. Bottleneck-phase analysis required. |
| **`INSTANT_PRO`** (GFT) | `gft_100k_live`, `gft_100k_phase1_942/743/613` | `cron_instructions/instant_pro_gatekeeper.md` | **2% FLOATING per-trade cap** (mark-to-market) — kills positions on unrealized DD even before they close. **20% consistency rule** (no single day > 20% of cycle profits). 4% trailing total DD. 14-day payout cycle. Single rule set, no phases. |

**Gatekeeper invocation** (mirrors `prop_risk_gatekeeper.py:evaluate_magic`):

```python
# Pseudocode — adapt for kent's tool/spawn primitives
framework = INSTANT_PRO if account in INSTANT_PRO.scope_accounts else FTMO_2STEP
system_prompt = framework.prompt_path.read_text()

summary = summarize_magic(account, magic, deals, account_meta)
# summary contains: n_trades, sharpe, win_rate, expectancy, profit_factor,
#                   max_drawdown_pct, mae_pct_95, daily_dd_max, ...

user_message = render_user_message(summary, account_meta) + framework.closing_note

verdict = spawn_claude_subagent(
    model="opus",
    system=system_prompt,
    user=user_message,
)
# Returns dict with: verdict (PASS|CONDITIONAL|FAIL), bottleneck_phase (FTMO only),
#                    raw_response, math_check, recommendation
```

**Critical operational rule** (per CLAUDE.md): the gatekeeper MUST use
the Claude subscription (`claude -p --model opus`), NOT the API. Crons
are subscription-only.

**Why opus:** the gatekeeper does multi-step math against a complex rule
set. Sonnet has been observed to round numbers up to make a strategy
qualify; Opus does not. This is one of the few places where model
choice materially affects output quality.

### Step 6 — Combine + report

Final per-magic status = stricter of (reflection RED/YELLOW/GREEN, gatekeeper
PASS/CONDITIONAL/FAIL). Roll up to per-account → write report.

---

## Status thresholds reference

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

## Report format

Plain-text Discord output (do NOT call `discord_send` directly from a
cron — most cron systems forward stdout to the channel; calling tools
manually causes double-delivery):

```
⚠️ Prop Risk Sentinel — YYYY-MM-DD HH:MM UTC

🟢 ftmo_10k — STABLE
└ Magic 3456799: 🟢 GREEN — Sharpe +1.24 | Expectancy $0.41/trade | n=66
└ DD headroom: $920 / $1,000 (8% used)

🔴 gft_100k_live — DECAY
└ Magic 22: 🔴 RED — Sharpe -2.31 | Expectancy -$10.34/trade | n=71
└ DD headroom: $7,266 / $8,000 (9% used)
└ Gatekeeper (Instant Pro): FAIL — bottleneck = 2% floating per-trade cap on JPY-cross
└ ⚠️ WhatsApp: review mag 22 before next session

⏳ gft_100k_phase1_942 — INSUFFICIENT_DATA
└ Magic 18: ⏳ INSUFFICIENT_DATA — n=24 (need 30+, n=200 for full claims)
```

---

## State persistence

- Per-run state: `memory/prop_risk_state.json`
  - One entry per `(account, magic)` with last status + timestamp
  - Used to detect status transitions (don't re-alert if unchanged)
- Sentinel log: `memory/prop_risk_sentinel_log.jsonl`
  - Append-only log of every classification, useful for retrospective analysis
- Long-term magic-level memory: `memory/prop/<account>.md`
  - Human-readable timeline per account

**De-duplication rule:** only re-alert if status **changed** OR **4+ hours**
since last RED alert for the same magic.

---

## Error handling

| failure | response |
|---|---|
| MT5 MCP unreachable | retry 3× with 5s delay, then report `⚠️ MT5 DATA UNAVAILABLE` for affected accounts and continue with the rest |
| Single-account fetch fails | log error, mark account UNAVAILABLE, continue |
| Gatekeeper subagent fails | fall back to reflection-only classification, append `⚠️ Gatekeeper unavailable` to that magic's line |
| All accounts fail | output `⚠️ MONITOR FAILED — MT5 MCP unavailable` and stop; do not write state |
| Unexpected exception | catch, log to `memory/prop_risk_errors.jsonl`, send minimal Discord notice with traceback head |

---

## Broker-rule reference

| Broker | Max Total DD | Max Daily DD | Floating SL kill | Consistency rule |
|---|---|---|---|---|
| FTMO 2-Step | 10% (`$1k` on 10k / `$10k` on 100k) | 5% (anchored midnight CET balance, incl. floating) | none | none |
| GFT Instant Pro | 4% trailing on equity | none | **2% floating per-trade cap (mark-to-market)** | day > 20% of cycle profits = payout denied |

---

## Implementation notes for porting to kent

### What kent needs to add

1. **Tool: `prop_risk_pull`** — wraps the MT5 MCP calls in Step 2. Takes
   account list, returns structured `{account: {info, deals}}` dict.
2. **Tool: `prop_risk_stats`** — wraps the Ernest Chan calcs in Step 3.
   Pure Python, no LLM, no MCP.
3. **Tool: `prop_risk_classify`** — wraps the two-pass reflection in Step 4.
4. **Tool: `prop_risk_gatekeeper`** — spawns the Opus subagent with the
   broker-specific system prompt. Returns the verdict dict.
5. **Cron entry**: every 2h Mon–Fri 8–23 local, runs the orchestrator.
6. **State files** under `~/.kent/prop_risk/` mirroring the OpenClaw layout.

### What kent should NOT do

- **Do not embed broker passwords.** All credentials live in
  `mt5_accounts.json` on the Windows MCP server — kent only references
  account names.
- **Do not call `discord_send` from inside the cron handler.** Output via
  return-value / stdout; the cron system handles delivery.
- **Do not use the API for the gatekeeper.** Subscription-only via
  `claude -p --model opus` — see `CLAUDE.md` for the standing rule.
- **Do not run during off-hours.** Wastes MCP and gatekeeper budget;
  no new trade data to evaluate.

### Mathematical philosophy

- `n < 30` → no claims. Period.
- `Sharpe < -10` → structural failure, not variance.
- `P < 0.05` → 2σ rare event, worth investigating.
- Hard broker limits are the only non-statistical kill switches.
- Pass-1 + Pass-2 disagreement always resolves to **stricter**.
- Gatekeeper's job is to find the **bottleneck phase** (FTMO) or the
  **structural rule the strategy violates** (GFT), not to coach.

---

## Open questions for v2

- Adaptive thresholds: should `Sharpe < -2.0` shift based on the strategy's
  own historical baseline? Currently absolute.
- Per-magic baseline tracking: store rolling 30-day Sharpe and compare
  to spot 40%+ decay (already in reflection rules, but not all magics
  have enough history).
- Consistency-rule pre-warning: GFT's 20% rule kills payouts silently —
  add a daily-cycle check that warns when one day's P&L exceeds
  15% of cycle total.
- Macro-event awareness: integrate with Hydra signals so the sentinel
  can suppress alerts during known-volatile windows (FOMC, NFP, ECB).
- Auto-pause: when a magic goes RED, write a `halt_<account>_<magic>.flag`
  that the EAs read on init — close the loop without operator intervention.
