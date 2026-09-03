"""
ensemble.py — Weighted voting ensemble across SMA, RSI, and Momentum strategies.

How it works
------------
Each strategy returns a SignalResult with a signal ('BUY'/'SELL'/'HOLD') and
a confidence in [0.0, 1.0].

Weighted score:
    buy_score  = sum(weight_i * confidence_i  for strategies that say BUY)
    sell_score = sum(weight_i * confidence_i  for strategies that say SELL)

Final signal:
    BUY   if  buy_score  - sell_score  >= ensemble_buy_threshold
    SELL  if  sell_score - buy_score   >= ensemble_sell_threshold
    HOLD  otherwise

Default weights: SMA=0.35, RSI=0.35, Momentum=0.30 (sum = 1.0)
Default threshold: 0.30 for both BUY and SELL

The ensemble's own confidence = max(buy_score, sell_score), clipped to [0,1].
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Tuple

import pandas as pd

from .signals import SignalResult, StrategyConfig
from .sma_crossover import SMAStrategy
from .rsi_strategy import RSIStrategy
from .momentum_strategy import MomentumStrategy

_NAME = "Ensemble"


class EnsembleStrategy:
    """Weighted-vote ensemble of SMA, RSI, and Momentum strategies."""

    def __init__(self, config: StrategyConfig = None) -> None:
        self.config     = config or StrategyConfig()
        self._strategies: List[Tuple[object, float]] = [
            (SMAStrategy(self.config),      self.config.sma_weight),
            (RSIStrategy(self.config),      self.config.rsi_weight),
            (MomentumStrategy(self.config), self.config.momentum_weight),
        ]

    # ------------------------------------------------------------------
    def generate(self, ticker: str, history_df: pd.DataFrame) -> SignalResult:
        """
        Run all sub-strategies and combine via weighted vote.

        Parameters
        ----------
        ticker     : Ticker symbol.
        history_df : Price/volume history DataFrame.

        Returns
        -------
        SignalResult  with strategy_name='Ensemble'.
        """
        cfg = self.config
        ts  = datetime.now(timezone.utc)

        results: List[Tuple[SignalResult, float]] = []
        reasons: List[str] = []

        for strategy, weight in self._strategies:
            try:
                r = strategy.generate(ticker, history_df)
                results.append((r, weight))
                reasons.append(f"{r.strategy_name}({r.signal},{r.confidence:.2f})")
            except Exception as exc:
                reasons.append(f"{strategy.__class__.__name__}(ERROR:{exc})")

        buy_score  = sum(w * r.confidence for r, w in results if r.signal == "BUY")
        sell_score = sum(w * r.confidence for r, w in results if r.signal == "SELL")
        net = buy_score - sell_score

        reason_str = " | ".join(reasons)

        if net >= cfg.ensemble_buy_threshold:
            conf = min(1.0, buy_score)
            return SignalResult(
                ticker=ticker, signal="BUY",
                confidence=round(conf, 3),
                reason=f"Ensemble BUY (net={net:+.3f}) [{reason_str}]",
                strategy_name=_NAME, timestamp=ts,
            )

        if -net >= cfg.ensemble_sell_threshold:
            conf = min(1.0, sell_score)
            return SignalResult(
                ticker=ticker, signal="SELL",
                confidence=round(conf, 3),
                reason=f"Ensemble SELL (net={net:+.3f}) [{reason_str}]",
                strategy_name=_NAME, timestamp=ts,
            )

        return SignalResult(
            ticker=ticker, signal="HOLD",
            confidence=0.0,
            reason=f"Ensemble HOLD (net={net:+.3f}, below thresholds) [{reason_str}]",
            strategy_name=_NAME, timestamp=ts,
        )
