# Forex "Advanced ML" Strategy (SIM-only A/B)

**File**: [`forex/strategy_advanced_ml.py`](../forex/strategy_advanced_ml.py)
**Runner key**: `"advanced_ml"`
**Type**: machine learning — regularized logistic regression + regime + trend filters
**Where it runs**: **SIM only** (by omission from both LIVE allowlists — `--account live --strategy advanced_ml` hard-errors).
**Momentum pre-filter**: **yes** (like `ml` — not in `_NO_MOMENTUM_FILTER`).
**Added**: 2026-08-30 — user-supplied design, run in parallel with the original [`ml`](forex_ml_strategy.md) (untouched) for an A/B comparison.

> Not to be changed. `ml` is also not to be changed.

---

## What's different from `ml`

| | `ml` | `advanced_ml` |
|---|---|---|
| Target | next-**1**-day close up? | **5-day** forward move ≥ `±0.75 × ATR` — sub-threshold moves are **NaN** (excluded from training as noise) |
| Training window | 126 bars | **252** bars |
| Regularization | none | **L2** (`l2 = 0.01`), 500 epochs, lr 0.03 |
| Features | 7 | **10** — RSI, ADX, BB%B, 1- & 5-day returns, EMA20/50 spread, price/EMA200, EMA20 5-day slope, RSI 3-day delta, ATR vs its 100-day mean |
| Confidence threshold | 0.58 | **0.62** (more selective) |
| Regime filter | ADX ≥ 20 only | ADX ≥ **22** **and** ATR percentile in **[0.20, 0.85]** (not too quiet, not abnormally volatile) |
| Trend confirmation | `close vs EMA200` | full **EMA stack** (`EMA5 > EMA20 > EMA50`, `close > EMA200`, `+DI > −DI`, `RSI ≥ 52`) — mirror for shorts |
| Stop management | runner's generic breakeven | its own `update_stop_price()` — breakeven at +1 ATR profit, then trail at +2 ATR (2×ATR trail) |
| `MIN_BARS` | 336 | **492** → required `CHART_BARS` 340 → **500** |

`RISK_PCT` (0.25%), ATR stop (2.0×), time stop (20d), lot round (1,000) are the same as `ml`.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Training window | 252 bars | `LOOKBACK` |
| Forecast horizon | 5 bars | `FORECAST_HORIZON` |
| Target threshold | 0.75 × ATR | `TARGET_ATR_MULT` |
| Confidence threshold | 0.62 | `CONFIDENCE_THRESHOLD` |
| ADX minimum | 22 | `ADX_MIN` |
| ATR-percentile band | 0.20 – 0.85 | `VOL_PCT_MIN` / `VOL_PCT_MAX` (over `VOL_LOOKBACK` = 252) |
| ATR stop multiple | 2.0× | `ATR_STOP_MULT` |
| Breakeven / trail triggers | +1.0 ATR / +2.0 ATR profit; trail = 2.0×ATR | `BREAKEVEN_TRIGGER_ATR` / `TRAIL_TRIGGER_ATR` / `TRAIL_ATR_MULT` |
| Risk per trade | 0.25% (`RISK_PCT = 0.0025`) | |
| Time stop | 20 calendar days | `TIME_STOP_DAYS` |
| Min bars | `200 + 252 + 40` = 492 | `MIN_BARS` |
| Runner slots | `_SWING_SLOTS` (184 — uncapped, mirrors `ml`) | |

---

## Model

Pure numpy L2 logistic regression:

```
w, b = 0
for 500 epochs:
    p      = sigmoid(X·w + b)
    err    = p − y
    grad_w = Xᵀ·err / n  +  0.01 · w      # L2
    w     -= 0.03 · grad_w
    b     -= 0.03 · err.mean()
```

Trained fresh per pair per run on the 252-bar window ending 5 bars back
(the last 5 bars have unknown 5-day futures). Features z-scored against the
training window. Returns `None` (pair skipped) if < 60 clean rows or the
target has only one class.

---

## Entry — all must hold

| | Long | Short |
|---|---|---|
| Model | `P ≥ 0.62` | `P ≤ 0.38` |
| Regime | `ADX(14) ≥ 22` and ATR percentile in [0.20, 0.85] and `ATR > 0` | same |
| Trend stack | `EMA5 > EMA20 > EMA50`, `close > EMA200`, `+DI > −DI`, `RSI ≥ 52` | `EMA5 < EMA20 < EMA50`, `close < EMA200`, `−DI > +DI`, `RSI ≤ 48` |

- `score` = `P` (Buy) / `1 − P` (Sell).
- `stop_price` = `close ∓ 2.0 × ATR(14)`.
- Signal carries `ml_prob` and `strategy: "advanced_ml"`.

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 20` | `time_stop (Nd)` |
| B | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |
| C | Model flips past the opposite threshold | `ml_flip (prob=…)` |

## Stop management — `update_stop_price(position, df)`

Wired generically into `forex/runner.py`'s `_run_exits` (2026-08-30). Local
`pos["stop_price"]` ratchet — same contract as `trailing_stop_update`;
broker-side sync via the breakeven amend + stop invigilator, identical to
every other trailing strategy.

- **+1.0 ATR profit** → stop to breakeven (entry).
- **+2.0 ATR profit** → stop trails at `close ∓ 2.0 × ATR` (never loosens).

## Known integration notes

- **`CHART_BARS` raised 340 → 500** to satisfy `MIN_BARS = 492`. Every other
  strategy reads converged span-≤200 indicators at `iloc[-1]` — unchanged
  signals; the only cost is a slightly larger daily-bar fetch per pair.
- **No `--scan` panel** — `python forex/runner.py --scan` has hardcoded
  panels and `advanced_ml` isn't one. Use `scan_summary()` directly, or the
  dashboard, or the run log. It still trades normally.
- Runs twice per cycle (agreement pre-scan + `_run_entries`), like `ml` —
  the heaviest strategy in the loop now (252-bar window, 500 epochs, L2).

## First live behaviour

Functional test (2026-08-30, 8 real pairs): trained fine on 505 bars,
produced 1 signal (NZDUSD Buy, P=0.727). GBPUSD had P=0.821 but was
**correctly rejected** by the regime filter (ATR percentile 0.01 < 0.20).

## Tests

`python test_2026_08_30_advanced_ml_strategy.py` → 7 pass.
