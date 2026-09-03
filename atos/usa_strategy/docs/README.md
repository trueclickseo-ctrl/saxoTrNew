# USA Strategy Package — Overview

## What is this?

The `usa_strategy` package is the **signal generation engine** of the Avanza USA Quant Trading Bot. It takes historical price/volume data for any stock and returns a **BUY**, **SELL**, or **HOLD** signal with a confidence score (0–1).

---

## Package Structure

```
usa_strategy/
├── __init__.py          # Package entry point — exports all public classes
├── signals.py           # Core data contracts (Signal, SignalResult, StrategyConfig)
├── sma_crossover.py     # Strategy 1: SMA Crossover + Volume + Trend filter
├── rsi_strategy.py      # Strategy 2: RSI(14) Mean Reversion
├── momentum_strategy.py # Strategy 3: Rate-of-Change + 52-week Breakout
├── ensemble.py          # Ensemble: weighted vote across all 3 strategies
└── docs/
    ├── README.md            ← this file
    ├── signals.md           ← data contracts documentation
    ├── sma_crossover.md     ← SMA strategy deep-dive
    ├── rsi_strategy.md      ← RSI strategy deep-dive
    ├── momentum_strategy.md ← Momentum strategy deep-dive
    ├── ensemble.md          ← Ensemble voting algorithm
    └── configuration.md     ← All tuneable parameters
```

---

## Quick Start

```python
import yfinance as yf
from usa_strategy import generate_signal, StrategyConfig

# Fetch data
df = yf.download("AAPL", period="2y", interval="1d", auto_adjust=True, progress=False)
df.columns = [c.lower() for c in df.columns]
df["price"] = df["close"]
df = df.reset_index().rename(columns={"date": "timestamp"})

# Default ensemble signal
result = generate_signal("AAPL", df)
print(result.signal)      # "BUY" | "SELL" | "HOLD"
print(result.confidence)  # 0.0 – 1.0
print(result.reason)      # Human-readable explanation

# Custom configuration
config = StrategyConfig(
    sma_short_window=5,
    sma_long_window=20,
    rsi_oversold=25.0,
    ensemble_buy_threshold=0.20,
)
result = generate_signal("NVDA", df, config=config)
```

---

## Signal Flow

```
Historical OHLCV Data (DataFrame)
          │
          ▼
  ┌───────────────┐    ┌──────────────┐    ┌──────────────────┐
  │  SMAStrategy  │    │ RSIStrategy  │    │ MomentumStrategy │
  │  weight=0.35  │    │ weight=0.35  │    │   weight=0.30    │
  └───────┬───────┘    └──────┬───────┘    └────────┬─────────┘
          │ SignalResult       │ SignalResult          │ SignalResult
          └────────────────────┴──────────────────────┘
                                │
                                ▼
                      ┌──────────────────┐
                      │ EnsembleStrategy │
                      │  Weighted Vote   │
                      └────────┬─────────┘
                               │
                    BUY / SELL / HOLD + confidence
```

---

## All Strategies at a Glance

| Strategy | Signal Trigger | Best For |
|---|---|---|
| **SMA Crossover** | Short MA crosses Long MA + volume + trend | Trending markets |
| **RSI** | RSI crosses out of oversold/overbought zone | Range-bound markets |
| **Momentum** | Positive ROC across 2 timeframes or 52w breakout | Strong trending stocks |
| **Ensemble** | Weighted combination of all 3 | General use (recommended) |

---

## Dependencies

- `pandas` — data manipulation
- `numpy` — numerical calculations
- No external TA libraries (TA-Lib, ta, pandas-ta) — all indicators implemented from scratch

---

## Files Changed in Backtest / Live Use

- `backtest.py` — uses this package for walk-forward testing
- `usa_paper_main.py` — calls `generate_signal()` every 5 minutes per stock
- `usa_paper_dashboard.py` — displays live signals on the Signals tab
