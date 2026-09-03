# Strategy Configuration Reference

## StrategyConfig — All Tuneable Parameters

The `StrategyConfig` dataclass controls every tuneable parameter across all strategies. Create one instance and pass it to any strategy or the ensemble.

```python
from usa_strategy import StrategyConfig, EnsembleStrategy

# Use all defaults
config = StrategyConfig()

# Custom config
config = StrategyConfig(
    sma_short_window       = 5,       # faster SMA
    sma_long_window        = 20,      # tighter crossover
    rsi_oversold           = 25.0,    # stricter oversold threshold
    ensemble_buy_threshold = 0.25,    # slightly more aggressive
    momentum_weight        = 0.40,    # bias toward growth stocks
    sma_weight             = 0.30,
    rsi_weight             = 0.30,
)

ensemble = EnsembleStrategy(config)
result   = ensemble.generate("NVDA", df)
```

---

## Full Parameter Reference

### SMA Crossover Parameters

| Parameter | Type | Default | Min valid | Max valid | Description |
|---|---|---|---|---|---|
| `sma_short_window` | int | **10** | 2 | < `sma_long_window` | Fast MA lookback (days) |
| `sma_long_window` | int | **50** | > `sma_short_window` | any | Slow MA lookback (days) |
| `sma_trend_window` | int | **200** | 1 | any | Trend filter MA (days) |
| `sma_volume_window` | int | **20** | 1 | any | Volume average window (days) |

### RSI Parameters

| Parameter | Type | Default | Min valid | Max valid | Description |
|---|---|---|---|---|---|
| `rsi_period` | int | **14** | 2 | any | RSI calculation period (days) |
| `rsi_oversold` | float | **30.0** | 0 | < `rsi_overbought` | BUY zone threshold |
| `rsi_overbought` | float | **70.0** | > `rsi_oversold` | 100 | SELL zone threshold |

### Momentum Parameters

| Parameter | Type | Default | Min valid | Max valid | Description |
|---|---|---|---|---|---|
| `mom_roc_short` | int | **5** | 1 | any | Short-term ROC lookback (days) |
| `mom_roc_long` | int | **20** | 1 | any | Long-term ROC lookback (days) |
| `mom_breakout_window` | int | **252** | 1 | any | 52-week high lookback (trading days) |
| `mom_volume_surge` | float | **1.5** | 1.0 | any | Volume multiplier for breakout confirm |
| `mom_overbought_roc` | float | **15.0** | 1.0 | any | Short-term ROC (%) → exhaustion SELL |

### Ensemble Parameters

| Parameter | Type | Default | Constraint | Description |
|---|---|---|---|---|
| `ensemble_buy_threshold` | float | **0.30** | > 0 | Net score needed to emit BUY |
| `ensemble_sell_threshold` | float | **0.30** | > 0 | Net score needed to emit SELL |
| `sma_weight` | float | **0.35** | [0,1] | SMA contribution to ensemble |
| `rsi_weight` | float | **0.35** | [0,1] | RSI contribution to ensemble |
| `momentum_weight` | float | **0.30** | [0,1] | Momentum contribution to ensemble |

> **Constraint:** `sma_weight + rsi_weight + momentum_weight` must equal exactly `1.0`

---

## Preset Configurations

### 1. Default (balanced, general purpose)
```python
config = StrategyConfig()
```

### 2. Aggressive — more trades, lower threshold
```python
config = StrategyConfig(
    ensemble_buy_threshold  = 0.20,
    ensemble_sell_threshold = 0.20,
)
```

### 3. Conservative — fewer, higher-conviction trades
```python
config = StrategyConfig(
    ensemble_buy_threshold  = 0.45,
    ensemble_sell_threshold = 0.45,
)
```

### 4. Growth Stock Focused (NVDA, PLTR, SMCI style)
```python
config = StrategyConfig(
    sma_short_window        = 5,
    sma_long_window         = 20,
    momentum_weight         = 0.50,
    sma_weight              = 0.25,
    rsi_weight              = 0.25,
    mom_overbought_roc      = 25.0,   # allow bigger moves before SELL
    ensemble_buy_threshold  = 0.25,
)
```

### 5. Mean-Reversion Focused (range-bound markets)
```python
config = StrategyConfig(
    rsi_weight              = 0.60,
    sma_weight              = 0.25,
    momentum_weight         = 0.15,
    rsi_oversold            = 25.0,
    rsi_overbought          = 75.0,
    ensemble_buy_threshold  = 0.25,
    ensemble_sell_threshold = 0.25,
)
```

### 6. Short-term Swing (5-day SMA)
```python
config = StrategyConfig(
    sma_short_window  = 5,
    sma_long_window   = 20,
    sma_trend_window  = 50,   # shorter trend filter for swing trading
    mom_roc_short     = 3,
    mom_roc_long      = 10,
)
```

---

## Validation Rules

`StrategyConfig.__post_init__` enforces these constraints at construction:

```python
assert sma_short_window < sma_long_window        # fast MA must be faster than slow
assert 0 < rsi_oversold < rsi_overbought < 100   # logical RSI thresholds
assert abs(sma_weight + rsi_weight + momentum_weight - 1.0) < 1e-6  # weights sum to 1
```

If any constraint is violated, a `ValueError` is raised immediately.
