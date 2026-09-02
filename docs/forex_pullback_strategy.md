# Forex Trend-Pullback-to-EMA(20) Strategy

> **RETIRED 2026-09-02** — a 12y / 49-CORE-pair decomposition
> ([strategy_decomposition_2026-09-02.md](strategy_decomposition_2026-09-02.md))
> found it net-negative with no rescuing filter. In `RETIRED_STRATEGIES`:
> exits still managed, opens nothing new. Rules below describe it as it last
> ran.

**File**: [`forex/strategy_pullback.py`](../forex/strategy_pullback.py)
**Runner key**: `"pullback"`
**Type**: trend-following, discounted re-entry
**Where it runs**: **SIM only** (was briefly on the LIVE allowlist 2026-08-27, removed same week).
**Momentum pre-filter**: **yes**.

> Not to be changed. Described exactly as it runs today.

---

## Concept

Same trend as the EMA crossover strategy, but a **better entry price**: wait for
an established trend, let price pull back to the EMA(20) dynamic support, and
enter only once it has *bounced* back in the trend direction. Triple confirmation
(trend + pullback + bounce) is what drives the high claimed win rate.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Trend EMA | 50 | `TREND_EMA` |
| Pullback EMA | 20 | `PULLBACK_EMA` |
| ADX period / minimum | 14 / **25** | `ADX_PERIOD` / `ADX_MIN` |
| ATR period / stop multiple | 14 / **1.5×** (tight — entry is near support) | `ATR_PERIOD` / `ATR_STOP_MULT` |
| Risk per trade | **0.25%** (`RISK_PCT = 0.0025`; docstring says "1%" — stale) | |
| Time stop | 25 calendar days | `TIME_STOP_DAYS` |
| Pullback lookback | 3 bars | `PULLBACK_LOOKBACK` |
| Min bars | `50 + 14 + 5` = 69 | `MIN_BARS` |

---

## Entry — all three required

| | Long | Short |
|---|---|---|
| Trend | `close > EMA50` and `ADX(14) ≥ 25` | `close < EMA50` and `ADX(14) ≥ 25` |
| Pullback | any of the last ~3 bars had `low ≤ its EMA20` | any of last ~3 bars had `high ≥ its EMA20` |
| Bounce | `close > EMA20` now | `close < EMA20` now |

- `score` = current ADX.
- `stop_price` = `close ∓ 1.5 × ATR(14)` — deliberately tight because the entry
  sits right at EMA(20) support/resistance.

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 25` | `time_stop (Nd)` |
| B | Long: `close < EMA50` / Short: `close > EMA50` (major trend broke) | `trend_break (…)` |
| C | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |

No `trailing_stop_update` defined.

## Sizing

`size_position(equity, atr, min_units, risk_pct=None, block_below_min=False)` —
same shape as `bb` (risk_pct override + block_below_min both supported).

## Inspect

`python forex/runner.py --scan` → `[PULLBACK]` panel (`scan_summary` reports
trend / adx_ok / above_pb / pb_touch per pair).
