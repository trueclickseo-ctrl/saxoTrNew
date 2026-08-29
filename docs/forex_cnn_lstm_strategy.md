# Forex CNN-LSTM Deep-Learning Strategy

**File**: [`forex/strategy_cnn_lstm.py`](../forex/strategy_cnn_lstm.py) (inference) · [`forex/cnn_lstm_trainer.py`](../forex/cnn_lstm_trainer.py) (training)
**Runner key**: `"cnn_lstm"`
**Type**: deep learning — multi-scale CNN + BiLSTM + self-attention, 3-class (Sell / Hold / Buy)
**Where it runs**: **SIM only**.
**Momentum pre-filter**: **yes**.

> Not to be changed. Described exactly as it runs today.

---

## Status (measured, not aspirational)

Per the model's own `data/cnn_lstm/report.json`: walk-forward accuracy **36.9%**
on a 3-class problem (chance = 33.3%), and **signal rate 0.0 in all 5 folds** at
the old 0.58 threshold. `docs/forex_strategies.md` Strategy 10 flags it for
"lower the threshold and re-validate, or retire".

**The threshold is currently `CONFIDENCE_THRESHOLD = 0.45`** (not 0.58) — lower
than when that finding was written, so it *can* now emit occasional signals when
one softmax class clears 0.45 and ADX ≥ 15. It holds no positions historically.

---

## Model & data

- Trained once, offline, on **5 years of daily bars across all 34 pairs** (~40k
  sequences) via `cnn_lstm_trainer.py`. Persisted to `data/cnn_lstm/`
  (`model.pt`, `scaler`, `config`). Loaded lazily and cached in-process.
- Feature engineering (`build_features`, `N_FEATURES`, `SEQ_LEN`) lives in the
  trainer — single source of truth; the strategy imports it.
- Inference: take the last `SEQ_LEN` feature rows, normalise with the saved
  scaler, run the torch model, softmax → `(P_sell, P_hold, P_buy)`.
- If `data/cnn_lstm/model.pt` is missing, `generate_signals` returns `[]`
  silently. Train with `python -m forex.cnn_lstm_trainer --train`.

---

## Parameters

| Param | Value | Constant |
|---|---|---|
| Confidence threshold | **0.45** | `CONFIDENCE_THRESHOLD` |
| ADX minimum | **15** (lowest of any strategy) | `ADX_MIN` |
| ATR period / stop multiple | 14 / **2.5×** | `ATR_PERIOD` / `ATR_STOP_MULT` |
| Risk per trade | **0.25%** (`RISK_PCT = 0.0025`; docstring says "1%" — stale) | |
| Time stop | 15 calendar days | `TIME_STOP_DAYS` |
| Min bars | `220 + SEQ_LEN` | `MIN_BARS` |

---

## Entry

| | Long | Short |
|---|---|---|
| Model | `P(Buy) ≥ 0.45` | `P(Sell) ≥ 0.45` |
| Trend | `ADX(14) ≥ 15` | same |

- Hold class is never traded.
- `score` = the winning class probability.
- `stop_price` = `close ∓ 2.5 × ATR(14)`.
- Signal carries `prob_buy` / `prob_sell` / `prob_hold` / `adx`.

## Exit — `should_exit()`, first hit

| # | Condition | Reason |
|---|---|---|
| A | `days_held ≥ 15` | `time_stop (Nd)` |
| B | Long: `low ≤ stop` / Short: `high ≥ stop` | `hard_stop (px)` |
| C | Model flips: Long with `P(Sell) ≥ 0.45`, Short with `P(Buy) ≥ 0.45` | `model_flip … p=…` |

No `trailing_stop_update` defined.

## Sizing

`units = floor(equity_in_quote × 0.0025 / (2.5 × ATR) / 1000) × 1000`,
`block_below_min` supported.

## Inspect

```bash
python -m forex.cnn_lstm_trainer --status     # model / training state
python forex/runner.py --scan                 # [CNN-LSTM] panel — p_sell/p_hold/p_buy/adx
```

## See also

- The other, much lighter ML strategy: [forex_ml_strategy.md](forex_ml_strategy.md)
  (logistic regression, retrained per-run, actually fires regularly).
