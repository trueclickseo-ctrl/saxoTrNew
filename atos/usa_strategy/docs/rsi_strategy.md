# RSI Strategy — Deep Dive

## Overview

The **Relative Strength Index (RSI)** strategy is a momentum oscillator that measures the speed and change of price movements. It generates **mean-reversion signals** — buying when a stock has been oversold and selling when it is overbought.

---

## RSI Formula (Wilder's Method)

RSI is calculated using **Wilder's Exponential Weighted Moving Average** (EWM), which is equivalent to an alpha = 1/period smoothing.

### Step 1 — Price changes

```
delta[t] = price[t] - price[t-1]
gain[t]  = max(delta[t], 0)
loss[t]  = max(-delta[t], 0)
```

### Step 2 — Smoothed average gain and loss (Wilder EWM)

```
alpha = 1 / period          # default: 1/14 = 0.0714

avg_gain[t] = alpha * gain[t] + (1 - alpha) * avg_gain[t-1]
avg_loss[t] = alpha * loss[t] + (1 - alpha) * avg_loss[t-1]
```

### Step 3 — Relative Strength and RSI

```
RS[t]  = avg_gain[t] / avg_loss[t]
RSI[t] = 100 - (100 / (1 + RS[t]))
```

RSI ranges from **0 to 100**:
- RSI near **100** → all recent days were up → overbought
- RSI near **0**   → all recent days were down → oversold

---

## Signal Logic

The strategy detects **crossovers** of the RSI thresholds, not just levels:

### BUY — Oversold Recovery

```
if RSI[t-1] < oversold_threshold AND RSI[t] >= oversold_threshold:
    signal = BUY
```

This fires when RSI was below 30 (oversold) and now crosses back above 30 — indicating the selling pressure is exhausting and a recovery is beginning.

### SELL — Overbought Exhaustion

```
if RSI[t-1] > overbought_threshold AND RSI[t] <= overbought_threshold:
    signal = SELL
```

Fires when RSI was above 70 (overbought) and drops back below 70 — momentum is fading, mean reversion likely.

### HOLD

All other cases (RSI in neutral zone 30–70, or no crossover detected).

---

## Confidence Scoring

```
# BUY confidence — how deep into oversold territory was the previous bar?
depth      = max(0, oversold - RSI[t-1])
confidence = min(1.0, 0.1 + (depth / oversold) * 0.9)

# Example: RSI[t-1] = 20, oversold = 30
# depth = 10, confidence = 0.1 + (10/30)*0.9 = 0.40
```

```
# SELL confidence — how high above overbought was the previous bar?
height     = max(0, RSI[t-1] - overbought)
confidence = min(1.0, 0.1 + (height / (100 - overbought)) * 0.9)
```

---

## Visual Example

```
RSI
100 │
 70 │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ OVERBOUGHT ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ SELL fires here ↓
    │                                                    ╲
 50 │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ NEUTRAL ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ╲ ─ ─ ─ ─
    │         ╱                                             ╲
 30 │ ─ ─ ─ ─╱─ ─ ─ ─ ─ ─ OVERSOLD ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
  0 │ ╲     ╱                                  BUY fires here ↑
    └──────────────────────────────────────────────────────► Time
```

---

## Minimum Data Requirements

- **16 bars** minimum (14 for RSI period + 2 for crossover detection)
- Approximately **3 weeks** of daily data

---

## Parameters (tunable via StrategyConfig)

| Parameter | Default | Effect |
|---|---|---|
| `rsi_period` | 14 | Days for RSI calculation. Lower = more sensitive, more signals |
| `rsi_oversold` | 30.0 | Threshold below which stock is considered oversold |
| `rsi_overbought` | 70.0 | Threshold above which stock is considered overbought |

---

## Strengths & Weaknesses

| Strengths | Weaknesses |
|---|---|
| Works well in range-bound / sideways markets | Can give false BUY signals in strong downtrends (RSI stays low) |
| Reacts quickly — only needs 16 bars | Can give false SELL signals in strong uptrends (RSI stays high) |
| No external libraries needed | Misses pure trend-following opportunities |

---

## Example Output

```python
SignalResult(
    ticker        = "TSLA",
    signal        = "BUY",
    confidence    = 0.55,
    reason        = "RSI crossed above 30 (prev=22.4 -> now=31.1)",
    strategy_name = "RSI",
    timestamp     = datetime(2025, 9, 3, 10, 0, 0, tzinfo=UTC),
)
```
