# Forex "Advanced EMA" Strategy (SIM-only A/B)

**File**: [`forex/strategy_advanced_ema.py`](../forex/strategy_advanced_ema.py)
**Runner key**: `"advanced_ema"`
**Type**: trend-following — EMA(5/30) crossover, heavily filtered
**Where it runs**: **SIM only** (by omission from both LIVE allowlists — `--account live --strategy advanced_ema` hard-errors).
**Momentum pre-filter**: **yes** (like `ema`).
**Added**: 2026-08-30 — user-supplied design, run in parallel with the original [`ema`](forex_ema_strategy.md) (`forex/strategy.py`, untouched) for an A/B comparison.

> Not to be changed. `ema` is also not to be changed.

---

## What's different from `ema`

| | `ema` | `advanced_ema` |
|---|---|---|
| Cross | EMA5/EMA30 | same |
| Macro trend | (none) | **+ `close` vs EMA50** confirmation |
| ADX regime | `ADX(14) ≥ 25` | ADX ≥ 25 **and rising** (`ADX_now ≥ ADX[−3]`) |
| Volatility regime | (none) | **ATR percentile in [0.20, 0.90]** over 252 bars — skip dead & extreme regimes |
| Crossover age | within last 15 bars | within last **10** bars (`SIGNAL_LOOKBACK`) |
| Score | ADX alone | **composite**: `ADX + 0.25·|+DI − −DI| + 1000·|EMA5/EMA30 − 1|` |
| Signal carries | — | `cross_age`, `strategy: "advanced_ema"` |

`RISK_PCT` (0.25%), ATR stop (1.5×), time stop (45d), `trailing_stop_update` (1.5×ATR ratchet), and the crossover-reversal / hard-stop exits are **identical** to `ema`.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Fast / slow / trend EMA | 5 / 30 / **50** | `FAST_EMA` / `SLOW_EMA` / `TREND_EMA` |
| ADX period / minimum | 14 / 25 | `ADX_PERIOD` / `ADX_MIN` |
| ADX rising-check window | 3 bars | `ADX_RISING_BARS` |
| ATR period / stop multiple | 14 / **1.5×** | `ATR_PERIOD` / `ATR_STOP_MULT` |
| ATR-percentile band | 0.20 – 0.90 | `VOL_PCT_MIN` / `VOL_PCT_MAX` (over `VOL_LOOKBACK` = 252) |
| Crossover lookback | 10 bars | `SIGNAL_LOOKBACK` |
| Risk per trade | 0.25% (`RISK_PCT = 0.0025`) | |
| Time stop | 45 calendar days | `TIME_STOP_DAYS` |
| Min bars | `max(50, 252) + 14 + 10` = **276** | `MIN_BARS` |
| Runner slots | `_SWING_SLOTS` (184 — uncapped, mirrors `ema`) | |

No `CHART_BARS` change needed — 276 is inside the 500 daily bars the runner
fetches (raised to 500 for `advanced_ml`).

---

## Entry — all must hold

| | Long | Short |
|---|---|---|
| Fresh cross | EMA5 crossed **above** EMA30 within the last 10 bars | crossed **below** within 10 bars |
| Alignment now | `EMA5 > EMA30` | `EMA5 < EMA30` |
| Macro trend | `close > EMA50` | `close < EMA50` |
| DI | `+DI > −DI` | `−DI > +DI` |
| ADX regime | `ADX(14) ≥ 25` **and** `ADX_now ≥ ADX[−3]` | same |
| Volatility regime | ATR percentile ∈ [0.20, 0.90] | same |

- `score` = `ADX + 0.25·|+DI − −DI| + 1000·|EMA5/EMA30 − 1|` (trend quality, not ADX alone).
- `stop_price` = `close ∓ 1.5 × ATR(14)`.

## Exit — `should_exit()`, first hit (same as `ema`)

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 45` | `time_stop (Nd)` |
| B | EMA5 crosses back through EMA30 | `crossover_reversal` |
| C | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |

## Stop management

`trailing_stop_update(current_stop, current_price, current_atr, direction)`
— standard hook name, **already called generically** by `forex/runner.py`
`_run_exits`. Long → `max(old, price − 1.5×ATR)`, short → `min(old, price +
1.5×ATR)`. No runner change was needed (unlike `advanced_ml`'s
`update_stop_price`).

## Sizing

`units = floor(equity_in_quote × 0.0025 / (1.5 × ATR) / 1000) × 1000`,
`block_below_min` supported (SIM never uses it).

## Known integration notes

- **No `--scan` panel** — that handler is hardcoded per strategy. Use
  `scan_summary()` / the dashboard / the run log. It still trades normally.
- Not added to `strategy_learner.STRATEGY_NAMES["forex"]` — consistent with
  the other A/B strategies; auto-initializes to weight 1.0 on first sighting.

## First live behaviour

Functional test (2026-08-30): trains cleanly on 505 real bars. **0 signals
on a 10-major sample** — most were filtered by the ATR-percentile floor
(late-Aug low volatility: EURUSD 0.06, GBPUSD 0.01, AUDUSD 0.11 — all <
0.20). A synthetic parameter sweep confirms it *does* fire on a valid
setup (Buy, cross_age 10, ADX 30.9). It is deliberately more selective
than the original `ema`.

## Tests

`python test_2026_08_30_advanced_ema_strategy.py` → 7 pass.
