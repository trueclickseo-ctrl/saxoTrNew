"""
usa_strategy — Quant signal generation for the Avanza USA bot.

Strategies (all return SignalResult):
    SMAStrategy      : SMA crossover with volume + trend filter
    RSIStrategy      : RSI(14) oversold/overbought reversals
    MomentumStrategy : Rate-of-change + 52-week breakout
    EnsembleStrategy : Weighted voting across all three

Quick usage
-----------
    from usa_strategy import EnsembleStrategy, StrategyConfig
    import pandas as pd

    config   = StrategyConfig()
    ensemble = EnsembleStrategy(config)
    result   = ensemble.generate("AAPL", history_df)
    print(result.signal, result.confidence, result.reason)
"""

from .signals import Signal, SignalResult, StrategyConfig
from .sma_crossover import SMAStrategy
from .rsi_strategy import RSIStrategy
from .momentum_strategy import MomentumStrategy
from .ensemble import EnsembleStrategy


def generate_signal(ticker: str, history_df, config: StrategyConfig = None) -> SignalResult:
    """
    Convenience wrapper: run the default ensemble on a history DataFrame.

    Parameters
    ----------
    ticker     : Stock ticker symbol.
    history_df : DataFrame with at minimum a 'price' (or 'close') column.
                 Optionally includes 'open', 'high', 'low', 'volume'.
    config     : StrategyConfig — uses defaults if None.

    Returns
    -------
    SignalResult
    """
    cfg = config or StrategyConfig()
    return EnsembleStrategy(cfg).generate(ticker, history_df)


__all__ = [
    "Signal",
    "SignalResult",
    "StrategyConfig",
    "SMAStrategy",
    "RSIStrategy",
    "MomentumStrategy",
    "EnsembleStrategy",
    "generate_signal",
]
