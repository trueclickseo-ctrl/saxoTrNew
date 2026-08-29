# Forex "Advanced RSI Master" Strategy (SIM-only A/B)

**File**: [`forex/strategy_advanced_rsi_master.py`](../forex/strategy_advanced_rsi_master.py)
**Runner key**: `"advanced_rsi_master"`
**Type**: mean-reversion — RSI(2) pullback inside a confirmed trend
**Where it runs**: **SIM only** — deliberately NOT in either LIVE allowlist. The LIVE_EUR account keeps running the original `rsi`; this is a shadow/A/B only.
**Momentum pre-filter**: **exempt** (added to `_NO_MOMENTUM_FILTER`, like the original `rsi`).
**Added**: 2026-08-30, user-supplied "master" design, parallel to the untouched [`rsi`](forex_rsi_strategy.md).

> Not to be changed. `rsi` is also not to be changed.

## What's different from `rsi`

| | `rsi` | `advanced_rsi_master` |
|---|---|---|
| RSI edge cases | plain | robust: pure-up→100, pure-down→0, flat→50 |
| Trend gate | `close vs EMA200` | `close vs EMA200` **+ EMA50 vs EMA200 + EMA200 10-bar slope** |
| Distance gate | none | `|close − EMA200| / ATR ≥ 0.35` (skip regime-boundary trades) |
| DI / ADX | none | `+DI > −DI` (long), `ADX(14) ≥ 18` |
| Volatility | none | ATR percentile ∈ **[0.15, 0.90]** over 252 bars |
| Confirmation | fires on the extreme | requires today's bar to confirm the pullback stopped (`close > prev_close` **and** `RSI rising`) — mirror for shorts |
| Score | RSI distance | `RSI extremity + RSI recovery + min(ADX,40)/200` |

Exit rules (`rsi_recovery` at 55/45, hard stop, 10‑day time stop), 1.5×ATR stop, 0.25% risk, and the **no-op trailing stop** are the same intent as `rsi` (time stop is 10d here vs 12d).

## Key params

`RSI_OVERSOLD 10 / RSI_OVERBOUGHT 90`, `MIN_TREND_DISTANCE_ATR 0.35`, `DI_ADX_MIN 18`, `VOL_PCT [0.15, 0.90]`, `TREND_SLOPE_BARS 10`, `TIME_STOP_DAYS 10`, `MIN_BARS 272`. `MAX_POSITIONS = 4` is in the file but **not enforced** (runner uses `_SWING_SLOTS`, same as the original `rsi`).

## Tests

`python test_2026_08_30_advanced_master_strategies.py` → 8 pass (covers all 4 master strategies).
