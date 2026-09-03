# Signal Data Contracts

## Overview

The `signals.py` module defines the **core data structures** that flow between all parts of the strategy package. Every strategy returns a `SignalResult`, and all strategies accept a `StrategyConfig`.

---

## Signal Type

```python
Signal = Literal["BUY", "SELL", "HOLD"]
```

A simple string literal type. Only these three values are valid.

---

## SignalResult

```python
@dataclass(frozen=True)
class SignalResult:
    ticker        : str       # e.g. "AAPL"
    signal        : Signal    # "BUY" | "SELL" | "HOLD"
    confidence    : float     # 0.0 (no confidence) to 1.0 (maximum confidence)
    reason        : str       # human-readable explanation
    strategy_name : str       # e.g. "SMAStrategy", "RSI", "Momentum", "Ensemble"
    timestamp     : datetime  # UTC datetime of the last bar used
```

### Key properties

| Field | Range | Notes |
|---|---|---|
| `confidence` | `[0.0, 1.0]` | Validated — `ValueError` if outside range |
| `signal` | `"BUY"/"SELL"/"HOLD"` | Validated — `ValueError` if anything else |
| `timestamp` | UTC datetime | Always timezone-aware |

### SignalResult is **frozen** (immutable)

Once created, a `SignalResult` cannot be modified. This prevents accidental mutation as results flow through the system.

### Usage

```python
result = strategy.generate("AAPL", df)

if result.signal == "BUY" and result.confidence >= 0.4:
    # High-confidence BUY — place order
    ...

if result.signal == "SELL":
    # Close position
    ...

# Log it
print(f"{result.ticker}: {result.signal} ({result.confidence:.0%}) — {result.reason}")
```

---

## StrategyConfig

```python
@dataclass
class StrategyConfig:
    # SMA
    sma_short_window  : int   = 10
    sma_long_window   : int   = 50
    sma_trend_window  : int   = 200
    sma_volume_window : int   = 20

    # RSI
    rsi_period        : int   = 14
    rsi_oversold      : float = 30.0
    rsi_overbought    : float = 70.0

    # Momentum
    mom_roc_short       : int   = 5
    mom_roc_long        : int   = 20
    mom_breakout_window : int   = 252
    mom_volume_surge    : float = 1.5
    mom_overbought_roc  : float = 15.0

    # Ensemble
    ensemble_buy_threshold  : float = 0.30
    ensemble_sell_threshold : float = 0.30
    sma_weight              : float = 0.35
    rsi_weight              : float = 0.35
    momentum_weight         : float = 0.30
```

See `configuration.md` for full reference, presets, and tuning guide.

---

## DataFrame Input Format

All strategies accept a `pandas.DataFrame` with these columns:

| Column | Required | Type | Notes |
|---|---|---|---|
| `price` | **Yes** | float | Closing price. Can be named `close` or `Close` — auto-converted |
| `timestamp` | **Yes** | datetime/str/int | Date of the bar. Auto-created from index if missing |
| `volume` | Optional | int/float | Required for volume-based filters. Skipped if absent |
| `close` | Optional | float | Alias for `price` |
| `open` | Optional | float | Not currently used by strategies |
| `high` | Optional | float | Not currently used by strategies |
| `low` | Optional | float | Not currently used by strategies |

### Minimum bars by strategy

| Strategy | Minimum bars | Why |
|---|---|---|
| SMA Crossover | **201** | 200-day trend MA + 1 crossover bar |
| RSI | **16** | 14-day period + 2 crossover bars |
| Momentum | **22** | 20-day long ROC + 2 buffer bars |
| Ensemble | **201** | Determined by slowest strategy (SMA) |

---

## Preparing Data from Yahoo Finance

```python
import yfinance as yf

df = yf.download("AAPL", period="2y", interval="1d", auto_adjust=True, progress=False)

# Flatten MultiIndex columns (yfinance sometimes returns these)
if hasattr(df.columns, "get_level_values"):
    df.columns = df.columns.get_level_values(0)

# Lowercase
df.columns = [str(c).lower() for c in df.columns]

# Add 'price' column
df["price"] = df["close"]

# Add 'timestamp' column from index
df = df.reset_index()
df = df.rename(columns={"date": "timestamp"})

# Now safe to pass to any strategy
result = strategy.generate("AAPL", df)
```

## Preparing Data from Local CSV (usa_data_client)

```python
from usa_data_client import read_history_df

df = read_history_df("AAPL")   # reads data/usa/AAPL.csv
# Columns: timestamp, ticker, price, prev_close, change_pct_1d, volume

result = strategy.generate("AAPL", df)
```
