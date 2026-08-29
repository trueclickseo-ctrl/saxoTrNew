# Forex "Donchian Quality" Strategy (SIM-only A/B)

**File**: [`forex/strategy_donchian_quality.py`](../forex/strategy_donchian_quality.py)
**Runner key**: `"donchian_quality"`
**Type**: breakout / trend-following — a filtered variant of [`donchian`](forex_donchian_strategy.md)
**Where it runs**: **SIM only** (by omission from both LIVE allowlists).
**Momentum pre-filter**: **yes**.
**Added**: 2026-08-29 (commit `f296b86`) — from a user design doc.
**Purpose**: run in parallel with the original `donchian` (untouched) and compare.

> Not to be changed. Original `donchian` is also not to be changed — the whole
> point is the A/B comparison.

---

## What's different from `donchian` (7 items, from the design doc)

| # | `donchian` | `donchian_quality` |
|---|---|---|
| 1 | breakout strength computed for **ranking only** | **filter**: `0.10 ≤ (close − channel)/ATR ≤ 1.50` — too small = noise, too large = exhausted/news |
| 2 | ADX just needs to be **≥ 25** | ADX must **also be rising**: `ADX_now > ADX[−3]` |
| 3 | no distance cap | **skip** if `(close − EMA200)/ATR > 3.0` (late/extended move) |
| 4 | ranks by unbounded breakout size | ranks within the bounded 0.10–1.50 ATR band (item 1 already removed the outliers) |
| 5 | `MAX_POSITIONS = 4` **never enforced** (runner uses `_SWING_SLOTS` = 184) | `SLOTS_PER_STRATEGY["donchian_quality"] = MAX_POSITIONS` → **real 4-position cap** |
| 6 | trailing stop already works generically | defines the identical `trailing_stop_update` — same generic mechanism |
| 7 | `scan_summary` mislabels the 30-bar channel `high20`/`low20` | correctly `high30`/`low30` |

Exit logic and sizing math are **identical** to `donchian` — the design doc only asked for entry-quality filters.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Breakout channel / exit channel | 30 / 15 days | `BREAKOUT_PERIOD` / `EXIT_PERIOD` |
| Trend EMA | 200 | `EMA_TREND` |
| ADX min / rising-lookback | 25 / 2 bars | `ADX_MIN` / `ADX_RISING_LOOKBACK` |
| Breakout-strength band | **0.10 – 1.50 ATR** | `MIN_BREAKOUT_ATR` / `MAX_BREAKOUT_ATR` |
| Max EMA200 distance | **3.0 ATR** | `MAX_EMA_DISTANCE_ATR` |
| ATR period / stop multiple | 14 / 2.0× | `ATR_PERIOD` / `ATR_STOP_MULT` |
| Risk per trade | 0.25% (`RISK_PCT = 0.0025`) | |
| Time stop | 30 calendar days | `TIME_STOP_DAYS` |
| Real position cap | **4** | `MAX_POSITIONS` (enforced) |
| Min bars | `219 + 2` | `MIN_BARS + ADX_RISING_LOOKBACK` |

---

## Entry — all required

| | Long | Short |
|---|---|---|
| Breakout | `close > max(prior 30 closes)` | `close < min(prior 30 closes)` |
| Macro trend | `close > EMA200` | `close < EMA200` |
| ADX | `≥ 25` **and** `> ADX[−3]` (rising) | same |
| Breakout strength | `0.10 ≤ (close − high30)/ATR ≤ 1.50` | `0.10 ≤ (low30 − close)/ATR ≤ 1.50` |
| EMA distance | `(close − EMA200)/ATR ≤ 3.0` | `(EMA200 − close)/ATR ≤ 3.0` |

- `score` = `breakout_strength` (within the bounded band).
- `stop_price` = `close ∓ 2.0 × ATR(14)`.

## Exit — identical to `donchian`

Time stop 30d · 2.0×ATR hard stop · 15-day reverse-channel trail (`donchian_exit`).

## Sizing / trailing stop

Identical math to `donchian` — `0.25% / (2.0 × ATR)`, floored to 1,000;
`trailing_stop_update` ratchets at 2.0×ATR.

## Current signal history

Added Fri 2026-08-29 ~00:58. Has produced **zero signals so far** — but the
plain `donchian` shows "No signals" on the identical weekend runs too; a
30-day breakout can't form on a static weekend market. First real test:
Monday. (See `memory/forex_donchian_quality_strategy_2026-08-29.md`.)

## Tests

`python test_2026_08_29_donchian_quality_strategy.py` → 16 pass.

## Inspect

`python forex/runner.py --scan` → the `[DONCHIAN]` panel is the original;
`donchian_quality` shares the same scan display shape (`high30`/`low30`).
