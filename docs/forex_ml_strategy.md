# Forex ML Strategy — Logistic Regression Signals

**File**: [`forex/strategy_ml.py`](../forex/strategy_ml.py)
**Runner key**: `"ml"` (in `forex/runner.py`'s `STRATEGIES` dict)
**Type**: data-driven / machine-learning entry strategy
**Where it runs**: **SIM only.** Not in `LIVE_ALLOWED_STRATEGIES` (`bb`) or
`LIVE_EUR_ALLOWED_STRATEGIES` (`rsi`), so it can never place a real-money order.
**Status**: active on SIM — fires regularly (thousands of `[ml]` log lines in
`data/forex_scheduler.log`).

---

## ⚠️ Two different "ML" things — don't confuse them

| | This strategy (`strategy_ml.py`) | The signal-filter ML meta-gate (`signal_filter.py`) |
|---|---|---|
| Role | **Generates** Buy/Sell signals, like any other strategy | **Scores / vetoes** signals that other strategies already produced |
| Model | numpy logistic regression, retrained every run | persisted sklearn model (`fx_meta_model.pkl`) |
| Trained on | 126 daily price bars per pair | actual closed-trade outcomes (win/loss) |
| Features | 7 technical indicators | per-strategy agreement flags + ADX/ATR%/day-of-week |
| Activation | always on (SIM) | needs `MIN_TRADES_FOR_ML = 150` labelled closed trades; currently inert (0/150 in most tiers) |
| Threshold | `CONFIDENCE_THRESHOLD = 0.58` | `ML_THRESHOLD = 0.58` |

There is also a **third** learning component, [`strategy_learner.py`](../strategy_learner.py)
(repo root, not `forex/`), which adjusts each strategy's slot allocation from
its realised P&L — unrelated to either of the above. This document is about
the first column only.

---

## Concept

For every pair, on every run:

1. Build a 7-feature matrix from the daily bars.
2. Take a **126-bar training window that ends yesterday** (today's bar is
   excluded — no look-ahead).
3. Label each training row `1` if the *next* day closed higher, else `0`.
4. Standardise the features (z-score against the training window's own
   mean/std) and fit a logistic regression by gradient descent.
5. Feed **today's** standardised feature row through the fitted model to get
   `P(up)`.
6. Trade only when the model is confident **and** the market is trending
   (ADX ≥ 20).

The model is **not persisted** — it is retrained from scratch for each pair on
each run. `CHART_BARS = 340` daily bars are fetched (enough for
`EMA(200) + 126 lookback + buffer`); a pair with fewer than
`MIN_BARS = 336` bars is silently skipped.

---

## Features (7, all normalised)

| # | Feature | Formula (from `_build_features`) | Captures |
|---|---|---|---|
| 1 | RSI | `RSI(14) / 100` | short-term momentum (0–1) |
| 2 | ADX | `ADX(14) / 100` | trend strength (0–1) |
| 3 | BB %B | `(close − lower) / (2 × band)` on `BB(20, 2)`, clipped 0–1 | position within the Bollinger band |
| 4 | EMA5/EMA20 | `(EMA5 / EMA20 − 1) × 100` | fast vs slow trend spread |
| 5 | EMA20/EMA50 | `(EMA20 / EMA50 − 1) × 100` | medium-term trend |
| 6 | Price/EMA200 | `(close / EMA200 − 1) × 100` | macro trend bias |
| 7 | ATR% | `ATR(14) / close × 100` | normalised volatility |

**Target** (training only): `close.diff().shift(-1) > 0` — did the *next* bar
close up? Computed only over rows `[len−127 : len−1]`, i.e. it never touches
today's or a future bar for the live prediction.

---

## Model

Pure numpy — **no scikit-learn dependency**:

```
w, b initialised to zeros
for 200 epochs:
    p   = sigmoid(X·w + b)
    err = p − y
    w  -= 0.05 · Xᵀ·err / n
    b  -= 0.05 · mean(err)
```

| Param | Value | Constant |
|---|---|---|
| Training window | 126 bars (~6 months) | `LOOKBACK` |
| Learning rate | 0.05 | (arg to `_logistic_regression`) |
| Epochs | 200 | (arg) |
| Min usable training rows | 30 (after NaN drop) | inline guard |
| Feature scaling | per-window z-score (`(x − μ) / (σ + 1e-8)`) | inline |

Returns `None` (pair skipped this run) if: fewer than 30 clean training rows,
or today's feature row contains a NaN.

---

## Entry

Signals are produced by `generate_signals(market_data, open_symbols)`:

| Direction | Conditions |
|---|---|
| **Buy**  | `P(up) ≥ 0.58` **and** `ADX(14) ≥ 20` **and** `ATR(14) > 0` |
| **Sell** | `P(up) ≤ 0.42` **and** `ADX(14) ≥ 20` **and** `ATR(14) > 0` |

- `score` = `P(up)` for a Buy, `1 − P(up)` for a Sell. Signals are returned
  sorted by `score` descending (most confident first).
- Each signal carries `stop_price = close ∓ 2.0 × ATR(14)` and `ml_prob`
  (the raw probability, also logged and fed to the signal-filter).
- A pair already held (`sym in open_symbols`) is skipped — one ML position
  per pair at a time.

**Downstream gates** (in `forex/runner.py`, same as every strategy): the ML
strategy **is** subject to the momentum pre-filter (it is *not* in
`_NO_MOMENTUM_FILTER`), so only the top-ranked trending pairs are considered
for ML entries. It also passes through `signal_filter.evaluate()` (consensus
+ the meta-ML gate), the currency-exposure cap, the opposing-position check,
the spread check, and portfolio heat / margin caps.

---

## Exit

`should_exit(position, df, calendar_days_held)` — first condition wins:

| # | Condition | Reason string |
|---|---|---|
| A | `calendar_days_held ≥ 20` | `time_stop (Nd)` |
| B | Long and `low ≤ stop_price`, or Short and `high ≥ stop_price` (2.0×ATR at entry) | `hard_stop (px)` |
| C | Model retrained now and flips: Long with `P(up) ≤ 0.42`, or Short with `P(up) ≥ 0.58` | `ml_flip (prob=…)` |

Exits run every cycle regardless of market hours; the stop is also placed as
a real broker-side order at entry (`saxo_order.place_with_stop`), so it does
not depend on a scheduled run firing.

---

## Sizing

`size_position(account_equity, atr, min_units=1000, block_below_min=False)`:

```
risk_amount   = account_equity × RISK_PCT        # RISK_PCT = 0.0025 (0.25%)
stop_distance = 2.0 × ATR(14)
raw           = risk_amount / stop_distance
qty           = floor(raw / min_units) × min_units
```

- `RISK_PCT = 0.0025` — **0.25%**, matching the other swing strategies (cut
  from 1% → 0.5% on 2026-08-22, then → 0.25% on 2026-08-24; the module
  docstring still says "1%" and is stale).
- `block_below_min=True` (LIVE/LIVE_EUR only) makes it return `0` instead of
  flooring up to `min_units` — but the ML strategy never runs on a LIVE
  account, so in practice it always floors up on SIM.
- ATR is in the pair's **quote** currency; `forex/runner.py` converts equity
  into the quote currency before calling this (`_equity_in_quote`), so every
  pair gets the same real risk.

---

## Slots & concurrency

`SLOTS_PER_STRATEGY["ml"] = _SWING_SLOTS = len(PAIRS)` (currently **184**) —
effectively uncapped; concurrent ML exposure is bounded by the shared
currency-exposure and portfolio-heat gates, not a per-strategy slot count.

---

## Inspecting it

```bash
# One-off signal scan — ML is panel 9
python forex/runner.py --scan

# Dashboard row (WR / PF / P&L, strategy-wise)
python forex_dashboard.py
```

`scan_summary(market_data)` returns `{symbol, close, ml_prob, status}` per
pair for the `--scan` panel and the dashboard.

---

## Known limitations

- **Retrains every pair every run** — ~184 logistic-regression fits per scan.
  Cheap individually (numpy, 200 epochs) but it is the slowest strategy in
  the loop.
- **No walk-forward validation is surfaced.** The docstring's "~57–62% WR" is
  an estimate, not a measured figure from this codebase. Judge it from the
  live SIM dashboard's per-strategy P&L, not the docstring.
- **126 training samples** is small for a 7-feature model; the fit is
  deliberately shallow (fixed 200 GD epochs, no regularisation, no early
  stopping) so it is more a "weighted indicator blend that re-tunes weekly"
  than a heavyweight model.
- **Zero persistence / no online learning** — each run starts from
  `w = 0`. A regime change is picked up only as the 126-bar window rolls
  forward.
- Not to be used as the meta-filter — that is `signal_filter.py`'s separate
  model (see the table at the top).

---

## Related files

| File | Role |
|---|---|
| [`forex/strategy_ml.py`](../forex/strategy_ml.py) | this strategy |
| [`forex/strategy_cnn_lstm.py`](../forex/strategy_cnn_lstm.py) | the heavier deep-learning strategy (⚠️ never fires at `CONFIDENCE = 0.58` — see `docs/forex_strategies.md` Strategy 10) |
| [`forex/signal_filter.py`](../forex/signal_filter.py) | the ML **meta-gate** (`ml_probability`, `passes_ml`, `retrain`) |
| [`strategy_learner.py`](../strategy_learner.py) | P&L-driven slot-allocation learner (repo root) |
| `docs/forex_strategies.md` (Strategy 9) | shorter summary, kept for the full-strategy comparison table |
