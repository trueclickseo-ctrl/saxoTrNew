# Forex Z-Score Mean-Reversion Strategy

**File**: [`forex/strategy_zscore.py`](../forex/strategy_zscore.py)
**Runner key**: `"zscore"`
**Type**: mean-reversion (a stricter BB-reversion variant)
**Where it runs**: **SIM only**.
**Momentum pre-filter**: **no** (in `_NO_MOMENTUM_FILTER`) — reversion strategy, scans the full universe.

> Not to be changed. Described exactly as it runs today.

---

## Concept

Instead of a fixed Bollinger band, use the actual **z-score**: how many standard
deviations price is from its 20-day mean. Beyond ±2σ is statistically
overextended → fade it, targeting reversion to the mean. A loose EMA(200) gate
(±1%) keeps it from fading a genuine trend breakout.

Uses **sample** std (`ddof=1`).

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Lookback (mean/std window) | 20 | `LOOKBACK` |
| Entry z-score | **±2.0** | `Z_ENTRY` |
| Exit z-score | **±0.3** (reverted) | `Z_EXIT` |
| Trend EMA | 200 | `EMA_TREND` |
| ATR period / stop multiple | 14 / **2.5×** (widest of the reversion strategies) | `ATR_PERIOD` / `ATR_STOP_MULT` |
| Risk per trade | **0.25%** (`RISK_PCT = 0.0025`; docstring says "1%" — stale) | |
| Time stop | 12 calendar days | `TIME_STOP_DAYS` |
| Min bars | `200 + 20 + 5` = 225 | `MIN_BARS` |

Docstring claims ~63% win rate — treat as an estimate; judge from the live SIM dashboard.

---

## Entry

| | Long | Short |
|---|---|---|
| Extreme | `z < −2.0` | `z > +2.0` |
| Trend zone | `close > EMA200 × 0.99` (not in a deep downtrend) | `close < EMA200 × 1.01` (not in a strong uptrend) |

- `score` = `abs(z)` (most extreme first).
- `stop_price` = `close ∓ 2.5 × ATR(14)`.

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 12` | `time_stop (Nd)` |
| B | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |
| C | Long: `z ≥ −0.3` / Short: `z ≤ +0.3` (back to mean) | `zscore_reverted (…)` |

No `trailing_stop_update` defined.

## Sizing

`units = floor(equity_in_quote × 0.0025 / (2.5 × ATR) / 1000) × 1000`,
`block_below_min` supported.

## Inspect

`python forex/runner.py --scan` → `[ZSCORE]` panel (`*** OVERSOLD → LONG ***` /
`*** OVERBOUGHT → SHORT ***` flags at ±2.0).

## SIM-only A/B twin

| Twin | Difference | Doc |
|---|---|---|
| `zscore_quality` | **non-directional-market gate only**: keep a signal only if `|+DI−−DI| ≤ 14`; everything else delegated to this module. From a 12y decomposition — this module as a whole is a coin flip (+0.002 R, CI spans zero) but the low-DI-spread quartile ran +0.132 R, stable both halves (the *exact same* filter that works for `bb`). The decomposition also found 19/49 CORE pairs stable-positive both halves — losers are the NZD pairs + GBP-commodity crosses. | [forex_zscore_quality_strategy.md](forex_zscore_quality_strategy.md) |

SIM-only, never in a LIVE allowlist. `forex/strategy_zscore.py` is
byte-unchanged — it is the A/B control.
