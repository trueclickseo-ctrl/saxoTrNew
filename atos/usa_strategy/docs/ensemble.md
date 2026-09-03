# Ensemble Strategy — Weighted Vote Algorithm

## Overview

The **Ensemble Strategy** combines all three sub-strategies (SMA, RSI, Momentum) using a **weighted scoring system**. No single strategy controls the outcome — consensus is required to generate a BUY or SELL.

This makes the ensemble more robust: in markets where SMA is giving false signals, RSI or Momentum may hold it back. The final signal is only fired when multiple strategies agree.

---

## How It Works — Step by Step

### Step 1 — Run all sub-strategies

Each strategy independently generates a `SignalResult` with:
- `signal`: `"BUY"`, `"SELL"`, or `"HOLD"`
- `confidence`: float in `[0.0, 1.0]`

```python
strategies = [
    (SMAStrategy(config),      weight=0.35),
    (RSIStrategy(config),      weight=0.35),
    (MomentumStrategy(config), weight=0.30),
]

results = [strategy.generate(ticker, history_df) for strategy, weight in strategies]
```

---

### Step 2 — Compute weighted scores

```
buy_score  = Σ (weight_i × confidence_i)  for all strategies where signal_i == "BUY"
sell_score = Σ (weight_i × confidence_i)  for all strategies where signal_i == "SELL"
net        = buy_score - sell_score
```

**Example:**
```
SMA      → BUY,  confidence=0.42, weight=0.35 → contributes 0.147 to buy_score
RSI      → HOLD, confidence=0.00, weight=0.35 → contributes 0.000
Momentum → BUY,  confidence=0.41, weight=0.30 → contributes 0.123 to buy_score

buy_score  = 0.147 + 0.123 = 0.270
sell_score = 0.000
net        = +0.270
```

---

### Step 3 — Apply thresholds

```
if net >= buy_threshold:
    signal = BUY,  confidence = min(buy_score, 1.0)

elif -net >= sell_threshold:
    signal = SELL, confidence = min(sell_score, 1.0)

else:
    signal = HOLD, confidence = 0.0
```

Default thresholds: **buy_threshold = 0.30**, **sell_threshold = 0.30**

In the example above: `net = 0.270 < 0.30` → **HOLD** (just short of threshold — good, prevents marginal trades).

---

## Weight Rationale

| Strategy | Weight | Why |
|---|---|---|
| SMA Crossover | **35%** | Reliable in trending markets — the most common US market regime |
| RSI | **35%** | Strong at catching reversals — complements SMA in range-bound markets |
| Momentum | **30%** | Excellent for high-growth stocks; slightly lower weight as it can be noisy |

Sum = 1.00 ✅ (enforced by `StrategyConfig.__post_init__`)

---

## Visual: Voting Matrix

```
Market State       │ SMA    │ RSI    │ Momentum │ Ensemble Result
───────────────────┼────────┼────────┼──────────┼────────────────
Strong uptrend     │ BUY    │ HOLD   │ BUY      │ → BUY  (0.35+0.30=0.65 net)
Overbought pullbk  │ HOLD   │ SELL   │ SELL     │ → SELL (0.35+0.30=0.65 net)
Sideways market    │ HOLD   │ HOLD   │ HOLD     │ → HOLD (0.00 net)
SMA false signal   │ BUY    │ HOLD   │ HOLD     │ → HOLD (only 0.35 net < 0.30)
All agree          │ BUY    │ BUY    │ BUY      │ → BUY  (1.00 net, high conf)
```

---

## Error Handling

If a sub-strategy raises an exception (e.g., bad data), the ensemble **does not crash**. It logs the error and excludes that strategy's contribution:

```python
try:
    r = strategy.generate(ticker, history_df)
    results.append((r, weight))
except Exception as exc:
    reasons.append(f"{strategy.__class__.__name__}(ERROR:{exc})")
    # continues with remaining strategies
```

---

## Parameters (tunable via StrategyConfig)

| Parameter | Default | Effect |
|---|---|---|
| `ensemble_buy_threshold` | 0.30 | Lower = more BUY signals (more trades, more risk) |
| `ensemble_sell_threshold` | 0.30 | Lower = more SELL signals |
| `sma_weight` | 0.35 | Increase for trending market bias |
| `rsi_weight` | 0.35 | Increase for mean-reversion bias |
| `momentum_weight` | 0.30 | Increase for growth stock focus |

> **Note:** `sma_weight + rsi_weight + momentum_weight` must equal exactly `1.0` — validated at construction time.

---

## Tuning Guide

| Goal | Change |
|---|---|
| More trades (aggressive) | Lower both thresholds to 0.20 |
| Fewer trades (conservative) | Raise both thresholds to 0.40 |
| Focus on growth stocks | Raise `momentum_weight`, lower `sma_weight` |
| Focus on stable large-caps | Raise `sma_weight`, lower `momentum_weight` |
| Better in bear markets | Raise `rsi_weight` (oversold bounces) |

---

## Example Output

```python
SignalResult(
    ticker        = "NVDA",
    signal        = "BUY",
    confidence    = 0.65,
    reason        = "Ensemble BUY (net=+0.481) "
                    "[SMAStrategy(BUY,0.42) | RSI(HOLD,0.00) | Momentum(BUY,0.78)]",
    strategy_name = "Ensemble",
    timestamp     = datetime(2025, 9, 3, 10, 0, 0, tzinfo=UTC),
)
```
