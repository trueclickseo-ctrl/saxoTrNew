# Forex "London Breakout V2" Strategy (SIM-only A/B)

**File**: [`forex/strategy_london_breakout_v2.py`](../forex/strategy_london_breakout_v2.py)
**Runner key**: `"london_breakout_v2"`
**Type**: intraday day-trading — a reworked variant of [`london_breakout`](forex_london_breakout_strategy.md)
**Where it runs**: **SIM only**, on the **same dedicated LBO tasks** as the original (`run_lbo_london.bat` / `run_lbo_ny.bat` / `run_lbo_close.bat` were updated 2026-08-29 to `--strategy london_breakout,london_breakout_v2`).
**Capital**: shares the **same LBO day-trading book** as the original.
**Added**: 2026-08-29 (commit `f296b86`) — from a 7-point user design-doc review.

> Not to be changed. Original `london_breakout` is also not to be changed.

---

## First run: **Monday London open**

Both LBO strategies are stripped from the generic scan and only run on the LBO
tasks, which are **Mon–Fri**. The batch files were updated Fri 2026-08-29;
zero occurrences in any `data/lbo_*.log` yet — first opportunity is Monday
07:00 UTC. (See `memory/forex_lbo_v2_strategy_2026-08-29.md`.)

---

## What changed vs `london_breakout` (9 items, verified against the original's real source)

| # | | |
|---|---|---|
| 1 | **FIXED** range-hour boundary | original's `_session_range()` used `<= end_h` (inclusive) despite claiming `[start, end)`. For NY that wrongly included hour 12. V2 uses a genuinely exclusive end (`< end_h`) — Asian `[0,7)`, London-morning `[9,13)`. |
| 2 | **FIXED** R/R never actually 2:1 | original enters at `latest_close` (can be far past the boundary), stops at the opposite boundary, targets a fixed `2×range` → real R/R shrinks as the break extends. V2 adds an explicit `actual_rr = tp_dist / stop_dist ≥ 1.5` (`MIN_ACTUAL_RR`) check. |
| 3 | **FIXED** backwards scoring | original's `score = rng_pips / MAX_RANGE_PIPS` with a "tighter ranges score higher" comment computes the opposite. V2 uses a real `compression_score = 1 − normalized_range` combined with breakout strength. |
| 4 | **FIXED** re-signals the same breakout | original only checks `sym in open_symbols`; a position that closes mid-session while price is still beyond the boundary re-signals. V2 tracks `already_traded_sessions` (symbol + UTC date + session) via `data/lbo_v2_session_cooldown.json`. |
| 5 | **REDUCED** position cap | `MAX_LBO_POSITIONS = 4` (vs 28), really enforced via `SLOTS_PER_STRATEGY["london_breakout_v2"]`. Cuts worst-case exposure from 42% to 4 × 0.5% = **2% of the LBO book**. |
| 6 | **REDUCED** risk | `RISK_PCT = 0.005` (0.5%, vs 1.5%). |
| 7 | **REBUILT** volatility filter | `0.5 ≤ range_price / ATR ≤ 3.0` (`MIN/MAX_RANGE_ATR_RATIO`), replacing the weak `atr_pips < 5 → skip`. |
| 8 | **NEW** breakout-strength band | `0.10 ≤ breakout distance / ATR ≤ 0.50` (`MIN/MAX_BREAKOUT_ATR`) — kills tiny false breaks and keeps the entry close enough to the boundary that item 2's R/R check rarely has to reject. |
| 9 | **FIXED** fallback `size_position()` | had the same `equity / 10.7` fabricated-USDSEK-rate bug already fixed in the main sizing path. Per "Option A", removed entirely — returns 0 (skip) rather than mis-size. |

Exit logic (TP / stop / time-stop) is the same shape as the original — the review didn't flag exits.

---

## Parameters

| Param | V2 value | (original) |
|---|---|---|
| Risk per trade | **0.5%** of the LBO book | 1.5% |
| Position cap | **4** (enforced) | 28 |
| Range / ATR ratio band | 0.5 – 3.0 | (none — weak ATR check) |
| Breakout-strength band | 0.10 – 0.50 ATR | (none) |
| Min real R/R | 1.5 | (not checked) |
| Take-profit | 2.0 × range | 2.0 × range |
| Min / max range | 10 / 120 pips | same |
| Max / min units | 50,000 / 1,000 | same |
| Pairs | same 28 | same 28 |
| Session windows | Asian `[0,7)`, London-morning `[9,13)`, close 20:00 UTC | inclusive-end variants |

## Repeat-signal cooldown

`_load_lbo_v2_session_cooldown()` / `_mark_lbo_v2_session_traded()` in
`forex/runner.py` persist `data/lbo_v2_session_cooldown.json` (mirrors the
gap-cooldown pattern). Once a pair trades in a session on a UTC date, it's done
for that session-day regardless of how many times price re-crosses.

## Tests

`python test_2026_08_29_lbo_v2_strategy.py` → 24 pass.

## Inspect

Shares the `[LBO]` `--scan` panel shape.
