"""
signals.py - Core data contracts for the usa_strategy package.

Defines:
  - Signal       : Literal type alias  ('BUY' | 'SELL' | 'HOLD')
  - SignalResult  : Immutable result dataclass returned by every strategy.
  - StrategyConfig: Single configuration dataclass that holds tuneable
                    parameters for ALL strategies. Strategies pick only the
                    fields they need, so callers can manage one config object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

Signal = Literal["BUY", "SELL", "HOLD"]


# ---------------------------------------------------------------------------
# SignalResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SignalResult:
    """Immutable result returned by every strategy function.

    Attributes
    ----------
    ticker        : Stock ticker symbol (e.g. 'AAPL').
    signal        : 'BUY', 'SELL', or 'HOLD'.
    confidence    : Float in [0.0, 1.0].  Higher -> strategy is more certain.
    reason        : Human-readable explanation of the signal.
    strategy_name : Name of the strategy that produced this result.
    timestamp     : UTC timestamp of the *last bar* used to produce the signal.
    """

    ticker: str
    signal: Signal
    confidence: float
    reason: str
    strategy_name: str
    timestamp: datetime

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        if self.signal not in ("BUY", "SELL", "HOLD"):
            raise ValueError(f"signal must be BUY/SELL/HOLD, got {self.signal!r}")


# ---------------------------------------------------------------------------
# StrategyConfig
# ---------------------------------------------------------------------------

@dataclass
class StrategyConfig:
    """Tuneable parameters for all strategies in the usa_strategy package.

    SMA Crossover
    -------------
    sma_short_window   : Lookback bars for the fast moving average.
    sma_long_window    : Lookback bars for the slow moving average.
    sma_trend_window   : Lookback bars for the long-term trend filter MA.
    sma_volume_window  : Lookback bars for the volume confirmation average.

    RSI
    ---
    rsi_period         : Number of bars for RSI calculation (Wilder EWM).
    rsi_oversold       : RSI level below which the asset is considered oversold.
    rsi_overbought     : RSI level above which the asset is considered overbought.

    Momentum / Breakout
    -------------------
    mom_roc_short      : Short-term rate-of-change period (bars).
    mom_roc_long       : Long-term rate-of-change period (bars).
    mom_breakout_window: Lookback bars for the 52-week-high breakout check.
    mom_volume_surge   : Volume must exceed this multiple of its mean to confirm
                         a breakout.
    mom_overbought_roc : Short-term ROC threshold above which mean-reversion
                         SELL is triggered (%).

    Ensemble
    --------
    ensemble_buy_threshold : Net BUY-score margin required to emit a BUY signal.
    ensemble_sell_threshold: Net SELL-score margin required to emit a SELL signal.
    sma_weight         : Weight for SMA crossover in the ensemble.
    rsi_weight         : Weight for RSI in the ensemble.
    momentum_weight    : Weight for Momentum in the ensemble.
    """

    # SMA
    sma_short_window: int   = 10
    sma_long_window: int    = 50
    sma_trend_window: int   = 200
    sma_volume_window: int  = 20

    # RSI
    rsi_period: int         = 14
    rsi_oversold: float     = 30.0
    rsi_overbought: float   = 70.0

    # Momentum
    mom_roc_short: int       = 5
    mom_roc_long: int        = 20
    mom_breakout_window: int = 252        # approx 1 trading year
    mom_volume_surge: float  = 1.5        # 1.5x average volume
    mom_overbought_roc: float = 15.0      # %

    # Ensemble
    ensemble_buy_threshold: float  = 0.3
    ensemble_sell_threshold: float = 0.3
    sma_weight: float              = 0.35
    rsi_weight: float              = 0.35
    momentum_weight: float         = 0.30

    def __post_init__(self) -> None:
        if self.sma_short_window >= self.sma_long_window:
            raise ValueError(
                "sma_short_window must be strictly less than sma_long_window"
            )
        if not (0 < self.rsi_oversold < self.rsi_overbought < 100):
            raise ValueError(
                "rsi_oversold and rsi_overbought must satisfy 0 < oversold < overbought < 100"
            )
        weights = self.sma_weight + self.rsi_weight + self.momentum_weight
        if abs(weights - 1.0) > 1e-6:
            raise ValueError(
                f"Ensemble weights must sum to 1.0, got {weights:.4f}"
            )