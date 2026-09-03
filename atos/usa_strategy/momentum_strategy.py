"""
momentum_strategy.py — Price momentum + 52-week breakout strategy.

Signal rules
------------
BUY  (breakout) : price breaks above its 252-bar high AND volume > 1.5x avg
BUY  (momentum) : both 20-day and 5-day ROC are positive (trend confirmation)
SELL (exhaustion): 5-day ROC > +15% — short-term overbought, mean-revert
HOLD            : all other cases

Priority: exhaustion-SELL > breakout-BUY > momentum-BUY > HOLD

Confidence
----------
Breakout BUY  : volume_ratio / (mom_volume_surge * 2), clipped to [0.1, 1.0]
Momentum BUY  : average of normalised roc_short and roc_long, clipped [0.1, 1.0]
Exhaustion SELL: (roc_short - threshold) / threshold, clipped [0.1, 1.0]
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from .signals import SignalResult, StrategyConfig

_NAME = "Momentum"


def _price_vol(df: pd.DataFrame):
    """Return (price_series, volume_series | None) from a DataFrame."""
    price = None
    for col in ("close", "Close", "price"):
        if col in df.columns:
            price = df[col].astype(float)
            break

    volume = None
    for col in ("volume", "Volume"):
        if col in df.columns:
            volume = df[col].astype(float)
            break

    return price, volume


class MomentumStrategy:
    """Rate-of-change + 52-week breakout momentum strategy."""

    def __init__(self, config: StrategyConfig = None) -> None:
        self.config = config or StrategyConfig()

    # ------------------------------------------------------------------
    def generate(self, ticker: str, history_df: pd.DataFrame) -> SignalResult:
        cfg              = self.config
        roc_short        = cfg.mom_roc_short        # default 5
        roc_long         = cfg.mom_roc_long         # default 20
        breakout_window  = cfg.mom_breakout_window  # default 252
        volume_surge     = cfg.mom_volume_surge     # default 1.5
        overbought_roc   = cfg.mom_overbought_roc   # default 15.0 %
        volume_window    = cfg.sma_volume_window    # default 20
        min_rows         = max(roc_long, volume_window) + 2

        ts = datetime.now(timezone.utc)

        def _hold(reason: str, conf: float = 0.0) -> SignalResult:
            return SignalResult(
                ticker=ticker, signal="HOLD", confidence=conf,
                reason=reason, strategy_name=_NAME, timestamp=ts,
            )

        # Normalise column names: ensure 'price' exists
        df = history_df
        if "price" not in df.columns and "close" in df.columns:
            df = df.copy()
            df["price"] = df["close"]
        elif "price" not in df.columns and "Close" in df.columns:
            df = df.copy()
            df["price"] = df["Close"]

        prices, volumes = _price_vol(df)
        if prices is None:
            return _hold("No usable price column in history_df.")
        if len(prices) < min_rows:
            return _hold(
                f"Insufficient data: {len(prices)} rows, need {min_rows}."
            )

        prices  = prices.reset_index(drop=True)
        price_now  = float(prices.iloc[-1])
        price_long = float(prices.iloc[-(roc_long + 1)])
        price_short = float(prices.iloc[-(roc_short + 1)])

        roc_long_val  = (price_now - price_long)  / price_long  * 100.0 if price_long  else 0.0
        roc_short_val = (price_now - price_short) / price_short * 100.0 if price_short else 0.0

        # Volume stats
        vol_ratio: Optional[float] = None
        if volumes is not None and len(volumes) >= volume_window:
            vol_mean  = float(volumes.iloc[-volume_window:].mean())
            vol_now   = float(volumes.iloc[-1])
            vol_ratio = (vol_now / vol_mean) if vol_mean > 0 else None

        # 52-week high (breakout window)
        window = min(breakout_window, len(prices) - 1)
        high_period = float(prices.iloc[-(window + 1):-1].max()) if window > 0 else price_now

        # ----------------------------------------------------------------
        # SELL — short-term exhaustion (highest priority)
        # ----------------------------------------------------------------
        if roc_short_val > overbought_roc:
            excess = roc_short_val - overbought_roc
            conf   = min(1.0, max(0.1, excess / overbought_roc))
            return SignalResult(
                ticker=ticker, signal="SELL", confidence=round(conf, 3),
                reason=(
                    f"Short-term overbought: {roc_short_val:.1f}% {roc_short}-day ROC "
                    f"exceeds {overbought_roc:.0f}% threshold"
                ),
                strategy_name=_NAME, timestamp=ts,
            )

        # ----------------------------------------------------------------
        # BUY — breakout above 52-week high with volume confirmation
        # ----------------------------------------------------------------
        if price_now > high_period and vol_ratio is not None and vol_ratio >= volume_surge:
            conf = min(1.0, max(0.1, vol_ratio / (volume_surge * 2)))
            return SignalResult(
                ticker=ticker, signal="BUY", confidence=round(conf, 3),
                reason=(
                    f"Breakout: ${price_now:.2f} > {window}-bar high ${high_period:.2f} "
                    f"with volume {vol_ratio:.1f}x avg"
                ),
                strategy_name=_NAME, timestamp=ts,
            )

        # ----------------------------------------------------------------
        # BUY — dual-timeframe momentum (both ROC positive)
        # ----------------------------------------------------------------
        if roc_long_val > 0 and roc_short_val > 0:
            # Confidence: average of each ROC normalised to a 10% reference
            ref = 10.0
            conf = min(1.0, max(0.1,
                0.5 * (min(roc_long_val, ref) / ref) +
                0.5 * (min(roc_short_val, ref) / ref)
            ))
            return SignalResult(
                ticker=ticker, signal="BUY", confidence=round(conf, 3),
                reason=(
                    f"Dual momentum: {roc_long}-day ROC={roc_long_val:+.1f}%, "
                    f"{roc_short}-day ROC={roc_short_val:+.1f}%"
                ),
                strategy_name=_NAME, timestamp=ts,
            )

        # ----------------------------------------------------------------
        # HOLD
        # ----------------------------------------------------------------
        return _hold(
            f"{roc_long}-day ROC={roc_long_val:+.1f}%, "
            f"{roc_short}-day ROC={roc_short_val:+.1f}% — no actionable signal."
        )
