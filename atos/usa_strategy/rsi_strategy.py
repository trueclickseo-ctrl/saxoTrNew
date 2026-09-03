"""
rsi_strategy.py — RSI(14) mean-reversion strategy.

Uses Wilder's smoothed EWM (equivalent to alpha = 1/period) to compute RSI
from scratch — no external TA libraries needed.

Signal rules
------------
BUY  : RSI crosses ABOVE rsi_oversold from below (oversold recovery)
SELL : RSI crosses BELOW rsi_overbought from above (overbought exhaustion)
HOLD : all other cases

Confidence
----------
BUY  : scales linearly from 0.1 (RSI just touched oversold) to 1.0 (RSI=0)
SELL : scales linearly from 0.1 (RSI just touched overbought) to 1.0 (RSI=100)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from .signals import SignalResult, StrategyConfig

_NAME = "RSI"


def _compute_rsi(prices: pd.Series, period: int) -> pd.Series:
    """
    Compute RSI using Wilder's EWM smoothing (alpha = 1/period).

    Returns a Series aligned with `prices`, with NaN for the first `period`
    rows where there is insufficient data.
    """
    delta = prices.diff()
    gain  = delta.clip(lower=0.0)
    loss  = (-delta).clip(lower=0.0)

    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False).mean()

    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # Mask the first `period` rows as NaN (warm-up)
    rsi.iloc[:period] = np.nan
    return rsi


def _price_series(df: pd.DataFrame) -> Optional[pd.Series]:
    """Extract the most appropriate price column from a DataFrame."""
    for col in ("close", "Close", "price"):
        if col in df.columns:
            return df[col].astype(float)
    return None


class RSIStrategy:
    """RSI(14) oversold/overbought mean-reversion strategy."""

    def __init__(self, config: StrategyConfig = None) -> None:
        self.config = config or StrategyConfig()

    # ------------------------------------------------------------------
    def generate(self, ticker: str, history_df: pd.DataFrame) -> SignalResult:
        """
        Generate a BUY / SELL / HOLD signal for `ticker`.

        Parameters
        ----------
        ticker     : Ticker symbol (informational only).
        history_df : DataFrame containing price data. Must have at least
                     rsi_period + 2 rows for a crossover to be detectable.

        Returns
        -------
        SignalResult
        """
        # Normalise column names: ensure 'price' exists
        df = history_df
        if "price" not in df.columns and "close" in df.columns:
            df = df.copy()
            df["price"] = df["close"]
        elif "price" not in df.columns and "Close" in df.columns:
            df = df.copy()
            df["price"] = df["Close"]
        cfg      = self.config
        period   = cfg.rsi_period
        oversold = cfg.rsi_oversold
        overbought = cfg.rsi_overbought
        min_rows = period + 2

        def _hold(reason: str, conf: float = 0.0) -> SignalResult:
            return SignalResult(
                ticker=ticker, signal="HOLD", confidence=conf,
                reason=reason, strategy_name=_NAME,
                timestamp=datetime.now(timezone.utc),
            )

        prices = _price_series(df)
        if prices is None:
            return _hold("No usable price column in history_df.")
        if len(prices) < min_rows:
            return _hold(
                f"Insufficient data: {len(prices)} rows, need {min_rows}."
            )

        rsi = _compute_rsi(prices, period)
        # Need at least two valid RSI values to detect a crossover
        valid = rsi.dropna()
        if len(valid) < 2:
            return _hold("Not enough RSI values after warm-up.")

        rsi_now  = float(valid.iloc[-1])
        rsi_prev = float(valid.iloc[-2])
        ts = datetime.now(timezone.utc)

        # ---- BUY: cross above oversold threshold ----------------------
        if rsi_prev < oversold and rsi_now >= oversold:
            # Distance from oversold: the further below it was, higher confidence
            depth = max(0.0, oversold - rsi_prev)   # how deep into oversold zone
            conf  = min(1.0, 0.1 + (depth / oversold) * 0.9)
            return SignalResult(
                ticker=ticker, signal="BUY", confidence=round(conf, 3),
                reason=(
                    f"RSI crossed above {oversold:.0f} "
                    f"(prev={rsi_prev:.1f} -> now={rsi_now:.1f})"
                ),
                strategy_name=_NAME, timestamp=ts,
            )

        # ---- SELL: cross below overbought threshold -------------------
        if rsi_prev > overbought and rsi_now <= overbought:
            height = max(0.0, rsi_prev - overbought)
            conf   = min(1.0, 0.1 + (height / (100.0 - overbought)) * 0.9)
            return SignalResult(
                ticker=ticker, signal="SELL", confidence=round(conf, 3),
                reason=(
                    f"RSI crossed below {overbought:.0f} "
                    f"(prev={rsi_prev:.1f} -> now={rsi_now:.1f})"
                ),
                strategy_name=_NAME, timestamp=ts,
            )

        # ---- HOLD -----------------------------------------------------
        zone = (
            f"overbought zone ({rsi_now:.1f})" if rsi_now >= overbought
            else f"oversold zone ({rsi_now:.1f})" if rsi_now <= oversold
            else f"neutral ({rsi_now:.1f})"
        )
        return _hold(f"RSI in {zone}, no crossover.", conf=0.0)
