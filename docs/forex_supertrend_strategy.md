# Forex SuperTrend Strategy

> **RETIRED 2026-09-02** — a 12y / 49-CORE-pair decomposition
> ([strategy_decomposition_2026-09-02.md](strategy_decomposition_2026-09-02.md))
> found it negative in 10 of 12 years, its own signal-strength ranking
> inverted, no rescuing filter. In `RETIRED_STRATEGIES`: exits still managed,
> opens nothing new.

**File**: [`forex/strategy_supertrend.py`](../forex/strategy_supertrend.py)
**Runner key**: `"supertrend"`
**Type**: trend-following
**Where it runs**: **SIM only**.
**Momentum pre-filter**: **yes**.

> Not to be changed. Described exactly as it runs today.

---

## Concept

SuperTrend(10, 3) builds a dynamic ATR band that flips between support and
resistance. A flip of the band's direction = a trend change. Enter on a fresh
flip, but only in the direction of the EMA(200) macro trend.

The SuperTrend line is computed with the standard iterative band-tightening
loop (numpy), with NaN-safe carry-forward for the warm-up period.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| ATR period | **10** | `ATR_PERIOD` |
| SuperTrend multiple | **3.0×** | `ST_MULT` |
| Trend EMA | 200 | `EMA_TREND` |
| ATR stop multiple | **2.0×** ATR(10) | `ATR_STOP_MULT` |
| Risk per trade | **0.25%** (`RISK_PCT = 0.0025`; docstring says "1%" — stale) | |
| Time stop | 40 calendar days | `TIME_STOP_DAYS` |
| Signal freshness | crossover within last 3 bars | `SIGNAL_LOOKBACK` |
| Min bars | `200 + 10 + 10` = 220 | `MIN_BARS` |

---

## Entry

| | Long | Short |
|---|---|---|
| Direction now | SuperTrend direction = `+1` (up) | `−1` (down) |
| Fresh flip | direction was `−1` somewhere in the last ~3 bars | was `+1` in the last ~3 bars |
| Macro trend | `close > EMA200` | `close < EMA200` |

- `score` = distance from the SuperTrend line in ATR units.
- `stop_price` = `close ∓ 2.0 × ATR(10)`.

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 40` | `time_stop (Nd)` |
| B | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |
| C | SuperTrend direction flips against the position | `supertrend_reversal` |

No `trailing_stop_update` defined (the SuperTrend line itself is the trail).

## Sizing

`units = floor(equity_in_quote × 0.0025 / (2.0 × ATR(10)) / 1000) × 1000`,
`block_below_min` supported.

## Inspect

`python forex/runner.py --scan` → `[SUPERTREND]` panel (direction / st_level /
ema200 / atr per pair).
