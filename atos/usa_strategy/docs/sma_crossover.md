# SMA Crossover Strategy — Deep Dive

## Overview

The **SMA (Simple Moving Average) Crossover** strategy is a classic trend-following approach enhanced with two quality filters: a volume confirmation gate and a long-term trend filter. These additions dramatically reduce false signals compared to a plain crossover.

---

## Algorithm

### Step 1 — Compute three moving averages

| MA | Default window | Purpose |
|---|---|---|
| Short MA | 10 days | Fast signal line |
| Long MA | 50 days | Slow signal line |
| Trend MA | 200 days | Long-term trend filter |

```
Short_MA[t] = mean(price[t-9 : t])
Long_MA[t]  = mean(price[t-49 : t])
Trend_MA[t] = mean(price[t-199 : t])
```

### Step 2 — Detect crossover

```
bullish_crossover = (Short_MA[t-1] <= Long_MA[t-1]) AND (Short_MA[t] > Long_MA[t])
bearish_crossover = (Short_MA[t-1] >= Long_MA[t-1]) AND (Short_MA[t] < Long_MA[t])
```

### Step 3 — Volume confirmation

```
Volume_MA[t] = mean(volume[t-19 : t])   # 20-day average volume
volume_confirmed = volume[t] > Volume_MA[t]
```

If no volume data is available, this filter is skipped (not blocking).

### Step 4 — Trend filter (BUY only)

```
above_trend = price[t] > Trend_MA[t]
```

Only BUY signals require the stock to be trading above its 200-day MA. This ensures we only buy in uptrends — not catching falling knives.

### Step 5 — Signal decision

```
if bullish_crossover AND volume_confirmed AND above_trend:
    signal = BUY

elif bearish_crossover AND volume_confirmed:
    signal = SELL

else:
    signal = HOLD
```

### Step 6 — Confidence score

```
distance_pct = |Short_MA - Long_MA| / Long_MA * 100
confidence   = min(distance_pct / 2.0, 1.0)
```

A 2% gap between MAs → full confidence (1.0). Smaller gaps = lower confidence.

---

## Visual Example

```
Price
 │                           ╭─── Short MA crosses above Long MA → BUY
 │               ╭──────────╯
 │    Long MA ───┤
 │               ╰──────── Short MA was below
 │
 └──────────────────────────────────────────► Time

         Trend MA (200d) must be BELOW current price for BUY to fire
```

---

## Minimum Data Requirements

- **201 bars** minimum (200 for trend MA + 1 for crossover comparison)
- Approximately **10 months** of daily data

---

## Parameters (tunable via StrategyConfig)

| Parameter | Default | Effect |
|---|---|---|
| `sma_short_window` | 10 | Faster = more signals, more noise |
| `sma_long_window` | 50 | Slower = fewer signals, less noise |
| `sma_trend_window` | 200 | Higher = stricter trend filter |
| `sma_volume_window` | 20 | Days to average volume for confirmation |

---

## Strengths & Weaknesses

| Strengths | Weaknesses |
|---|---|
| Works well in trending markets | Lags by nature — entry/exit never at exact top/bottom |
| Filters out many false signals via volume + trend | Many HOLD signals during sideways markets |
| Simple, interpretable | Needs 200+ bars — not suitable for newly-listed stocks |

---

## Example Output

```python
SignalResult(
    ticker     = "AAPL",
    signal     = "BUY",
    confidence = 0.42,
    reason     = "Bullish crossover: Short-MA(10)=185.3 > Long-MA(50)=182.1. "
                 "price=186.0 > 200d-MA=175.2. vol=98M vs 20d-avg=72M (x1.36).",
    strategy_name = "SMAStrategy",
    timestamp  = datetime(2025, 9, 3, 10, 0, 0, tzinfo=UTC),
)
```
