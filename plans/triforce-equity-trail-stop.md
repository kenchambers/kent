# Plan: Triforce account-level equity trail stop

## Context

Triforce shows a recurring failure mode across accounts: 2–3 weeks of small,
frequent gains (steady drip via individual trailing exits) followed by **one
catastrophic basket event** that erases multiple weeks of profit in a single
day. Confirmed in `logs/triforce_basket_distribution_60d.csv` (60d, 413
baskets, 6 accounts):

| account | mag | n baskets | best basket | **worst basket** | net trend |
|---|---|---|---|---|---|
| ftmo_10k | 3456799 | 66 | +$13.88 | −$4.23 | healthy |
| ftmo_100k | 25 | 92 | +$45.92 | −$21.12 | healthy |
| gft_613 | 18 | 82 | +$49.00 | −$14.43 | healthy |
| gft_743 | 25 | 78 | +$67.12 | −$108.74 | mild bleed |
| gft_100k_live | 21 | 71 | +$39.43 | **−$564.60** | **broken** |
| gft_942 | 18 | 24 | +$42.60 | **−$755.06** | **broken** |

The asymmetry on the broken accounts is roughly 6:1 between worst-basket
loss and peak-gains stretch. Mag 22's R:R fix (individual SL on, MaxGridLevels
3, EquityStop 0.10%) addresses **catastrophic-event prevention** — caps the
worst basket at ~$50 instead of ~$750. That's necessary but not sufficient:
even after that fix, there is no mechanism to **lock multi-day gains** when a
2-week winning streak gets eaten back by a slow series of small losers.

User's framing (2026-05-02): "we should be able to keep our gains from
previous 2 weeks. Not enough work is done to keeping gains when it moves
up." Asked for a trailing floating equity stoploss design with minimum
EA-surface-area change.

Decisions confirmed with user:
- **Place at the EA level**, not as an external cron, so the close-on-trip
  is synchronous with the equity reading.
- **Track peak from `ACCOUNT_BALANCE` (realized only)**; trip on
  `ACCOUNT_EQUITY` (real-time, includes unrealized) so adverse moves are
  caught mid-basket.
- **One input parameter only.** Hardcode sane defaults for everything else.
- **State must persist across EA restarts** via a per-magic file.
- **On trip: close only THIS magic's positions.** Other magics on the same
  account stay live.
- **Halt is permanent until manual reset** (delete state file or set input
  to 0). No auto-reset complexity in v1.

## Recommended approach

### 1. Add ONE input parameter

In the existing input block of the triforce EA:

```mql5
input double InpEquityTrailPct = 3.0;  // Trailing equity stop: halt magic if balance drops X% from peak (0 = disabled)
```

Setting `InpEquityTrailPct = 0` returns the EA to current behavior — safe to
ship dark, opt-in via per-account configuration.

### 2. Add TWO global state variables

```mql5
double   g_acctPeakBalance    = 0;     // running high-water mark of ACCOUNT_BALANCE since EA started
bool     g_equityTrailTripped = false;
```

State must survive EA restarts. Use a per-magic file path:

```mql5
string EquityTrailStateFile() {
   return StringFormat("kent_equity_trail_m%d.dat", InpMagicNumber);
}
```

Write `g_acctPeakBalance` and `g_equityTrailTripped` to this file every time
either changes. Read on `OnInit` (use `MQLInfoInteger(MQL_TESTER) ? 0 : restored_value`
to skip persistence in strategy tester).

### 3. Add ONE function called once per tick

Top of `OnTick` is fine (or once per minute via a throttle if tick load
matters):

```mql5
void CheckEquityTrail() {
   if (InpEquityTrailPct <= 0) return;                       // disabled
   if (g_equityTrailTripped) { CancelPendingForMagic(InpMagicNumber); return; }

   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double equity  = AccountInfoDouble(ACCOUNT_EQUITY);

   // Initialize peak on first call
   if (g_acctPeakBalance <= 0) { g_acctPeakBalance = balance; SaveTrailState(); return; }

   // Ratchet peak upward only on realized gains (BALANCE rises after a closed winner)
   if (balance > g_acctPeakBalance) { g_acctPeakBalance = balance; SaveTrailState(); }

   // Floor = peak minus trail %
   double floor = g_acctPeakBalance * (1.0 - InpEquityTrailPct / 100.0);

   // Trip on EQUITY (catches mid-basket adverse moves in real-time)
   if (equity < floor) {
      g_equityTrailTripped = true;
      SaveTrailState();
      Print("EQUITY TRAIL TRIP: equity=", equity, " < floor=", floor,
            " (peak=", g_acctPeakBalance, ", trail=", InpEquityTrailPct, "%)");
      CloseAllPositionsForMagic(InpMagicNumber);
      CancelPendingForMagic(InpMagicNumber);
   }
}
```

### 4. Add ONE gate at every order-opening site

Use the existing `InpMagicNumber` filter logic as a guide for where:

```mql5
if (g_equityTrailTripped) return;   // halt — equity trail was tripped
```

### 5. Helper functions to implement

Use existing patterns in the EA for iterating positions/orders by magic:

- `CloseAllPositionsForMagic(int magic)` — iterate `PositionsTotal()`, check
  magic, `PositionClose()` each
- `CancelPendingForMagic(int magic)` — iterate `OrdersTotal()`, check magic,
  `OrderDelete()` each
- `SaveTrailState()` — `FileOpen` with `FILE_WRITE|FILE_CSV`, write the two
  vars to `EquityTrailStateFile()`
- `LoadTrailState()` — `FileOpen` with `FILE_READ|FILE_CSV`, read the two
  vars; called in `OnInit`

## Behavior summary

- **Peak** tracks `BALANCE` (realized only) — never lowered, only ratcheted up.
- **Trip** uses `EQUITY` (real-time, includes unrealized) — fires fast during
  a bad move.
- **After trip:** all positions for THIS magic are closed, all pending
  orders cancelled, no new entries.
- **Halt is permanent** until you manually delete the state file or set
  `InpEquityTrailPct=0`.
- **One input controls everything.** `InpEquityTrailPct=3.0` ≈ 3% give-back
  tolerance.

## Worked example

A $100k account up 5% in one day with `InpEquityTrailPct=3.0`:

- Day starts: balance $100k, peak ratchets up as baskets close green
- Day ends: balance $105k, peak = $105k
- Floor now at $105k × 0.97 = **$101,850**
- Locked $1,850 of the $5k gain (the bottom 37%)
- Tomorrow if equity drops to $103k → above floor, fine
- If equity drops to $101.5k → trip fires, halt opens, **$1,500 of the
  original $5k preserved**

Tighter trail = more locked but more frequent trips on normal volatility.
Heuristic: trail % ≈ typical 1-day volatility × 1.5. For triforce on a normal
day that's been 0.5–1%, so 1.5–2% is the aggressive lock; 3% is the
conservative default.

## Do NOT change

- Existing trailing SL logic on individual legs
- Existing basket-TP / equity-stop logic
- Any existing input names or order
- Any existing state files

## Verification

- [ ] EA compiles with no warnings
- [ ] Setting `InpEquityTrailPct=0` returns the EA to current behavior
      (no peak tracking, no trip detection, no halt)
- [ ] Restart the EA mid-session: peak persists, halt state persists
- [ ] Backtest: simulate a +5% then −2% sequence; confirm halt fires at
      the right level
- [ ] Trip event: verify only the matching magic's positions close, other
      magics keep running
- [ ] State file lives at `<EA-files-dir>/kent_equity_trail_m<magic>.dat`
      and survives EA stop/start

## Open questions for v2 (out of scope here)

- Auto-reset after N days at peak (currently manual only)
- Per-symbol contribution attribution (which leg cost us into the trip?)
- External monitoring: a `kent` agent watch that DMs the user when a trip
  fires, including the basket details and the next-action recommendation
- Account-level (vs per-magic) trail when multiple triforce magics share
  the same account equity
