# Forex "Advanced BB Master" Strategy (SIM-only A/B)

**File**: [`forex/strategy_advanced_bb_master.py`](../forex/strategy_advanced_bb_master.py)
**Runner key**: `"advanced_bb_master"`
**Type**: mean-reversion — Bollinger-band exhaustion/reversion
**Where it runs**: **SIM only** (not in either LIVE allowlist).
**Momentum pre-filter**: **exempt** (`_NO_MOMENTUM_FILTER`, like the original `bb`).
**Added**: 2026-08-30, user-supplied "master" design, parallel to the untouched [`bb`](forex_bb_strategy.md).

> Not to be changed. `bb` is also not to be changed.

## What's different from `bb`

| | `bb` | `advanced_bb_master` |
|---|---|---|
| Trend ceiling | none | **`ADX(14) ≤ 30`** — skip strong directional trends / band-walks |
| Volatility | none | ATR percentile ∈ [0.20, 0.90] over 252 bars |
| Band-width | none | `(upper − lower)/|mid| ≥ 0.002` — reject near-flat bands |
| Excursion size | just "outside the band" | today outside the band **or** a prior excursion within the last 3 bars |
| Reversal | RSI extreme only | RSI extreme **+ today's candle reverses** (`close > prev_close` for long) **+ `close` still on the correct side of BB mid + DI agrees** |
| Score | ATR distance past band | `excursion(ATR) + RSI edge + (30 − ADX)/100` |

Exit (`bb_mid_reversion`, hard stop, **8‑day** time stop), 2.0×ATR stop, 0.25% risk, and `trailing_stop_update` (2.0×ATR ratchet) are the same as `bb`.

## Key params

`RSI_OB 65 / RSI_OS 35`, `ADX_MAX 30`, `MIN_EXCURSION_ATR 0.15`, `MIN_BANDWIDTH_PCT 0.002`, `REVERSAL_LOOKBACK 3`, `VOL_PCT [0.20, 0.90]`, `TIME_STOP_DAYS 8`, `MIN_BARS 282`. Uncapped slots (mirrors `bb`).

## Tests

`python test_2026_08_30_advanced_master_strategies.py` → 8 pass.
