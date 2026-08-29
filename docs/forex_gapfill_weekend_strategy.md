# Forex "GAPFILL Weekend" Strategy (SIM-only A/B)

**File**: [`forex/strategy_gap_weekend.py`](../forex/strategy_gap_weekend.py)
**Runner key**: `"gap_weekend"`
**Type**: gap-fade — a rebuilt variant of [`gap`](forex_gap_strategy.md)
**Where it runs**: **SIM only** (never in either LIVE allowlist).
**Momentum pre-filter**: **no** (`_NO_MOMENTUM_FILTER`).
**`NEEDS_LIVE_PRICES = True`**.
**Added**: 2026-08-29 (commit `f296b86`) — from a user design doc.
**Independent state**: own cooldown file `data/gap_weekend_cooldown.json`, own
`pnl_ledger.db` rows (`strategy="gap_weekend"`), own slots — fully isolated from `gap`.

> Not to be changed. Original `gap` is also not to be changed.

---

## Current behaviour: **weekly window only**

`ENABLED_SESSIONS = set()` (empty) → only the **weekly** gap trades. The
london/newyork/tokyo session logic is fully built (items 5–7 below) but
**dormant** pending review of weekly-only results. So in the logs you see
`[gap_weekend] Entries skipped — not in a gap session window` every run except
the Sunday 22:00 UTC → Monday 06:00 UTC window.

**First live window: Sunday night** (added the Friday before, no weekend has
passed yet — see `memory/forex_gapfill_weekend_strategy_2026-08-29.md`).

---

## What changed vs `gap` (8 items, from the design doc)

| # | | |
|---|---|---|
| 1 | **FIXED** sizing | `gap`'s `size_position()` always used the module-level `ATR_STOP_MULT` (1.5), so session-gap positions (real stop 2.0×) were undersized. Here `size_position(..., stop_mult, ...)` is a **required** parameter — the runner passes the same multiplier used for that signal's `stop_price` (1.5 weekly / 2.0 session). |
| 2 | **FIXED** reference bar | `gap`'s `_find_ref_bar_close()` silently fell back to "last close" when the true H1 reference bar was missing. Here a missing reference → `continue` (skip), never a guessed price. |
| 3 | **NEW** per-gap-type stats | every signal carries `gap_type` (`weekly`/`london`/`newyork`/`tokyo`), threaded into `pnl_tracker.log_open()`. `report_gap_weekend_by_type.py` reports WR / PF / expectancy **per type**, never combined. |
| 4 | **Phased rollout** | `ENABLED_SESSIONS` gates which sessions scan. Currently empty = weekly only. |
| 5 | **Rebuilt (dormant)** session detection | displacement measured in ATR units, not %-of-price: `0.8 ≤ |open − ref|/ATR(H1) ≤ 2.0`. |
| 6 | **Rebuilt (dormant)** reversal confirmation | requires the last completed H1 bar to already be a reversal candle in the fade direction — no confirming candle → skip. |
| 7 | **Rebuilt (dormant)** ranking | `_session_quality_score()` = composite of ATR displacement + reversal-candle strength + distance from the 20-bar extreme. Explicitly a **first-pass, unvalidated** ranking, to be revisited with real data. |
| 8 | **UNCHANGED** weekly logic | entry condition, exits, target = Friday close — kept as close to `gap`'s original as possible per "keep weekly simpler until you have separate stats". |

---

## Weekly parameters (identical to `gap`)

| Param | Value |
|---|---|
| Gap band | 0.10% – 2.00% of price |
| Stop | 1.5 × gap_size |
| Time stop | 7 calendar days |
| Risk | 0.25% |
| Target | Friday daily close (resting Limit order at entry) |

## Weekly entry / exit

Same as [`gap`](forex_gap_strategy.md) weekly: open above Friday close → Sell;
open below → Buy. Exit on target hit (checked on `cur_close`), 1.5×gap hard
stop, or 7-day time stop.

## Session parameters (dormant — for reference)

| Session | ref hour UTC | stop | time stop | ATR band |
|---|---|---|---|---|
| london | 06:00 | 2.0× | 8h | 0.8–2.0 |
| newyork | 11:00 | 2.0× | 6h | 0.8–2.0 |
| tokyo | 23:00 | 2.0× | 7h | 0.8–2.0 |

## Sizing

`size_position(account_equity, atr=gap_size, min_units, stop_mult, risk_pct, block_below_min)`
— **`stop_mult` is required**. `raw = equity × risk_pct / (stop_mult × gap_size)`.

## Tests

`python test_2026_08_29_gap_weekend_strategy.py` → 21 pass.
`python report_gap_weekend_by_type.py` → per-gap-type performance.

## Inspect

`python forex/runner.py --scan` → shares the `[GAP]` panel shape.
