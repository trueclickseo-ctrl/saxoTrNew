# Momentum Strategy — Deep Dive

## Overview

The **Momentum Strategy** captures directional price trends using **Rate-of-Change (ROC)** across two timeframes and identifies **52-week breakouts** with volume confirmation. It has three distinct signal modes with a clear priority order.

---

## Signal Modes (Priority: highest → lowest)

```
Priority 1: SELL — Short-term exhaustion (prevents riding a crash)
Priority 2: BUY  — 52-week breakout with volume surge
Priority 3: BUY  — Dual-timeframe momentum confirmation
Priority 4: HOLD — Default
```

---

## Algorithm

### Rate of Change (ROC)

```
ROC_short[t] = (price[t] - price[t - roc_short]) / price[t - roc_short] * 100
ROC_long[t]  = (price[t] - price[t - roc_long])  / price[t - roc_long]  * 100
```

Default: `roc_short = 5 days`, `roc_long = 20 days`

---

### Mode 1 — SELL: Short-term Exhaustion (highest priority)

```
if ROC_short > overbought_roc_threshold:
    signal = SELL
```

Default threshold: **+15%** in 5 days. If a stock has surged more than 15% in one week, it is likely overextended and due for a pullback.

**Confidence:**
```
excess     = ROC_short - threshold
confidence = min(1.0, max(0.1, excess / threshold))
```

---

### Mode 2 — BUY: 52-week Breakout with Volume

```
high_252d = max(price[t-252 : t-1])   # yesterday's 252-day high

if price[t] > high_252d AND volume[t] > (volume_surge * avg_volume):
    signal = BUY
```

Default `volume_surge = 1.5` → volume must be at least **1.5× the 20-day average**.

Breakouts above the 52-week high with above-average volume signal a genuine trend continuation — institutions are accumulating.

**Confidence:**
```
confidence = min(1.0, max(0.1, volume_ratio / (volume_surge * 2)))
# Example: volume 3× avg, surge threshold 1.5×
# confidence = min(1.0, max(0.1, 3.0 / 3.0)) = 1.0
```

---

### Mode 3 — BUY: Dual-Timeframe Momentum

```
if ROC_long > 0 AND ROC_short > 0:
    signal = BUY
```

Both the 20-day and 5-day momentum are positive — the stock is trending up on both timeframes.

**Confidence:**
```
ref        = 10.0    # reference ROC for full confidence
confidence = 0.5 * min(ROC_long / ref, 1.0) + 0.5 * min(ROC_short / ref, 1.0)
```

---

## Visual Example

```
Price
 │                                         ← 52w high broken with vol surge → BUY (Mode 2)
 │                              ╭──────────────
 │                         ────╯
 │  52w high ─ ─ ─ ─ ─ ─ ─│─ ─ ─ ─ ─ ─ ─ ─ ─ ─
 │                    ╭────╯
 │ ─ ─ ─ ─ ─ ─ ─ ─ ─ ╯
 └────────────────────────────────────────────► Time

Volume bar during breakout = 2.1× average → confirmed
```

---

## Minimum Data Requirements

- **22 bars** minimum (20 for long ROC + 2 buffer)
- 252 bars preferred for full breakout window (1 trading year)

---

## Parameters (tunable via StrategyConfig)

| Parameter | Default | Effect |
|---|---|---|
| `mom_roc_short` | 5 | Short-term lookback (days) |
| `mom_roc_long` | 20 | Long-term lookback (days) |
| `mom_breakout_window` | 252 | Historical high lookback (days) |
| `mom_volume_surge` | 1.5 | Volume multiplier required for breakout confirmation |
| `mom_overbought_roc` | 15.0 | 5-day ROC (%) threshold for exhaustion SELL |
| `sma_volume_window` | 20 | Days to average volume |

---

## Strengths & Weaknesses

| Strengths | Weaknesses |
|---|---|
| Catches powerful breakout moves early | False breakouts can trap long positions |
| Exhaustion SELL prevents holding through crashes | Dual-momentum generates many signals in bull markets |
| Works excellently for high-growth stocks (NVDA, PLTR, etc.) | Requires volume data for breakout mode |

---

## Example Output

```python
# Breakout BUY
SignalResult(
    ticker        = "NVDA",
    signal        = "BUY",
    confidence    = 0.78,
    reason        = "Breakout: $950.00 > 252-bar high $945.20 with volume 2.1x avg",
    strategy_name = "Momentum",
    timestamp     = datetime(2025, 9, 3, 10, 0, 0, tzinfo=UTC),
)

# Dual momentum BUY
SignalResult(
    ticker        = "AAPL",
    signal        = "BUY",
    confidence    = 0.41,
    reason        = "Dual momentum: 20-day ROC=+4.6%, 5-day ROC=+3.7%",
    strategy_name = "Momentum",
    timestamp     = datetime(2025, 9, 3, 10, 0, 0, tzinfo=UTC),
)

# Exhaustion SELL
SignalResult(
    ticker        = "TSLA",
    signal        = "SELL",
    confidence    = 0.34,
    reason        = "Short-term overbought: 22.3% 5-day ROC exceeds 15% threshold",
    strategy_name = "Momentum",
    timestamp     = datetime(2025, 9, 3, 10, 0, 0, tzinfo=UTC),
)
```
