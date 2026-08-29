# Forex EMA Crossover Strategy

**File**: [`forex/strategy.py`](../forex/strategy.py) (yes — the bare `strategy.py`, imported as `strat_ema`)
**Runner key**: `"ema"`
**Type**: trend-following
**Where it runs**: **SIM only** (LIVE = `bb`, LIVE_EUR = `rsi`).
**Momentum pre-filter**: **yes** (not in `_NO_MOMENTUM_FILTER`) — only top-ranked trending pairs are considered for entries.

> Not to be changed. This document describes it exactly as it runs today.

---

## Concept

A fast/slow EMA crossover, gated hard by ADX so it only fires when a real trend
exists. The ADX filter is the whole point — a bare EMA crossover "scissors"
constantly in a ranging pair; requiring `ADX ≥ 25` removes most of that whipsaw.

Entry is on **fresh alignment**, not the exact crossover bar: it looks back up
to 15 bars (3 trading weeks) for the crossover, so an established-but-still-intact
trend can still be entered.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Fast EMA | 5 | `FAST_EMA` |
| Slow EMA | 30 | `SLOW_EMA` |
| ADX period / minimum | 14 / **25** | `ADX_PERIOD` / `ADX_MIN` |
| ATR period | 14 | `ATR_PERIOD` |
| ATR stop multiple | **1.5×** | `ATR_STOP_MULT` |
| Risk per trade | **0.25%** (`RISK_PCT = 0.0025`) | docstring still says "1%" — stale |
| Time stop | 45 calendar days | `TIME_STOP_DAYS` |
| Crossover lookback | 15 bars | `SIGNAL_LOOKBACK` (inline) |
| Min bars | `SLOW_EMA + 14 + 5` = 49 | `MIN_BARS` |
| `MAX_POSITIONS` | 4 | **declared but not enforced** by the runner (uses `_SWING_SLOTS` = 184) |

Grid-optimal note in the file: 288-combo grid, 5y, 7 pairs → Sharpe 1.62, WR 56%, DD −5%.

---

## Entry

Both directions, all pairs. All must be true:

| | Long | Short |
|---|---|---|
| ADX | `ADX(14) ≥ 25` | same |
| Alignment now | `EMA5 > EMA30` | `EMA5 < EMA30` |
| Fresh crossover | EMA5 crossed **above** EMA30 within the last 15 bars | crossed **below** within 15 bars |
| DI confirm | `+DI > −DI` | `−DI > +DI` |

- `score` = current ADX (higher ADX ranked first).
- `stop_price` = `close ∓ 1.5 × ATR(14)`.

## Exit — `should_exit()`, first hit wins

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 45` | `time_stop (Nd)` |
| B | **Opposite crossover**: EMA5 crosses back through EMA30 | `crossover_reversal` |
| C | session low ≤ stop (long) / session high ≥ stop (short) | `hard_stop (px)` |

## Trailing stop

`trailing_stop_update()` ratchets the stop toward price only: long →
`max(old, price − 1.5×ATR)`, short → `min(old, price + 1.5×ATR)`. Called
generically by the runner for any strategy that defines it.

## Sizing

`units = floor(equity_in_quote × 0.0025 / (1.5 × ATR) / 1000) × 1000`.
Floors to the 1,000-unit micro-lot; returns `min_units` if smaller (or `0`
on LIVE with `block_below_min=True` — but EMA never runs LIVE).

## Inspect

`python forex/runner.py --scan` → `[EMA]` panel (`scan_summary` gives
close / fast / slow / gap% / ADX / ±DI / trend per pair).
