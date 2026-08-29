# Forex "Advanced CNN-LSTM Master" Strategy (SIM-only A/B)

**File**: [`forex/strategy_advanced_cnn_lstm_master.py`](../forex/strategy_advanced_cnn_lstm_master.py)
**Runner key**: `"advanced_cnn_lstm_master"`
**Type**: deep learning — **selection wrapper** around the existing CNN-LSTM model (no retrain)
**Where it runs**: **SIM only** (not in either LIVE allowlist).
**Momentum pre-filter**: **yes** (like the original `cnn_lstm`).
**Added**: 2026-08-30, user-supplied "master" design, parallel to the untouched [`cnn_lstm`](forex_cnn_lstm_strategy.md).

> Not to be changed. `cnn_lstm` is also not to be changed. **This does not retrain or recalibrate the model** — it loads the same `data/cnn_lstm/model.pt` (its own module-level `_cache`, no collision with `cnn_lstm`'s).

## What's different from `cnn_lstm`

| | `cnn_lstm` | `advanced_cnn_lstm_master` |
|---|---|---|
| Model | same `model.pt` | **same** `model.pt` — inference only |
| Trade gate | `P(class) ≥ 0.45` | `P(winner) ≥ 0.52` **and** `winner − runner-up ≥ 0.08` (`MIN_CLASS_MARGIN`) **and** `P(hold) ≤ 0.38` (`MAX_HOLD_PROB`) |
| Regime | ADX ≥ 15 | ADX ≥ **20** **and rising** (`ADX_now ≥ ADX[−3]`) **and** ATR percentile ∈ [0.20, 0.90] |
| Direction confirm | none | model direction must agree with `close vs EMA20 vs EMA50` **and** DI |
| Score | winning prob | `confidence + 0.75·class_margin + 0.01·ADX` |

Exit: hard stop, **15‑day** time stop, and `model_flip` only on a *decisive* opposite signal (same 0.52 + 0.08-margin gate). 2.5×ATR stop, 0.25% risk, `trailing_stop_update` (2.5×ATR).

## Key params

`CONFIDENCE_THRESHOLD 0.52`, `MIN_CLASS_MARGIN 0.08`, `MAX_HOLD_PROB 0.38`, `ADX_MIN 20`, `ADX_RISE_BARS 3`, `EMA_FAST/SLOW 20/50`, `VOL_PCT [0.20, 0.90]`, `TIME_STOP_DAYS 15`, `MIN_BARS 282` (= `max(220 + SEQ_LEN, 252 + 30)`). Uncapped slots (mirrors `cnn_lstm`).

## Note

Whether the stricter gates actually help must be validated out-of-sample —
the model itself is unchanged, so this is purely a trade-selection A/B.

## Tests

`python test_2026_08_30_advanced_master_strategies.py` → 8 pass.
