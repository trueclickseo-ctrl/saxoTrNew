# Forex "Advanced Pullback Master" Strategy (SIM-only A/B)

**File**: [`forex/strategy_advanced_pullback_master.py`](../forex/strategy_advanced_pullback_master.py)
**Runner key**: `"advanced_pullback_master"`
**Type**: trend-following — pullback-to-EMA20 continuation
**Where it runs**: **SIM only** (not in either LIVE allowlist).
**Momentum pre-filter**: **yes** (like the original `pullback` — trend-continuation).
**Added**: 2026-08-30, user-supplied "master" design, parallel to the untouched [`pullback`](forex_pullback_strategy.md).

> Not to be changed. `pullback` is also not to be changed.

## What's different from `pullback`

| | `pullback` | `advanced_pullback_master` |
|---|---|---|
| Trend structure | `close > EMA50` + ADX ≥ 25 | full **`close > EMA20 > EMA50`** + `EMA5 > EMA20` |
| Volatility | none | ATR percentile ∈ [0.20, 0.90] over 252 bars |
| ADX | ≥ 25 | ≥ 25 **and not fading** (`ADX_now ≥ ADX[−4]`) |
| DI | none | `+DI > −DI` (long) |
| Bounce | `close > EMA20` now | `close > EMA20` **and `close > prev_close`** (same-day up bar) |
| Touch | low ≤ EMA20 in last 3 bars | same (`_recent_touch`, `touch_age` reported) |
| Score | ADX alone | `ADX + 0.25·DI-edge + 1000·|EMA20/EMA50 − 1|` |

Exit (`trend_break` on `close vs EMA50`, hard stop, **25‑day** time stop), 1.5×ATR stop, 0.25% risk, and `trailing_stop_update` (1.5×ATR ratchet) are the same as `pullback`.

## Key params

`FAST_CONFIRM_EMA 5 / PULLBACK_EMA 20 / TREND_EMA 50`, `ADX_MIN 25`, `PULLBACK_LOOKBACK 3`, `VOL_PCT [0.20, 0.90]`, `TIME_STOP_DAYS 25`, `MIN_BARS 276`. Uncapped slots (mirrors `pullback`).

## Tests

`python test_2026_08_30_advanced_master_strategies.py` → 8 pass.
