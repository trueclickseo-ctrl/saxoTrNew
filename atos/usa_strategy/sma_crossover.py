"""
sma_crossover.py - Enhanced SMA crossover strategy.

Signal logic
------------
BUY  : Short MA crosses above Long MA  AND  volume > 20-day avg volume
       AND  price > 200-day MA  (trend filter)
SELL : Short MA crosses below Long MA  AND  volume > 20-day avg volume
HOLD : Everything else (or not enough data)

Confidence
----------
Proportional to the normalised distance between the two MAs at crossover.
Capped at 1.0.

    distance_pct = abs(short_ma - long_ma) / long_ma * 100
    confidence   = min(distance_pct / 2.0, 1.0)   # 2% gap -> full confidence

Usage
-----
    from usa_strategy.sma_crossover import SMAStrategy
    result = SMAStrategy(df, ticker='AAPL')
    result = SMAStrategy(df, ticker='AAPL', config=StrategyConfig(sma_short_window=5))
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .signals import Signal, SignalResult, StrategyConfig

_DEFAULT_CONFIG = StrategyConfig()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _rolling_mean(series: pd.Series, window: int) -> pd.Series:
    """Simple rolling mean; returns NaN for the first (window-1) elements."""
    return series.rolling(window=window, min_periods=window).mean()


def _last_timestamp(df: pd.DataFrame) -> datetime:
    """Extract the last row timestamp as a tz-aware UTC datetime."""
    ts = df["timestamp"].iloc[-1]
    if isinstance(ts, pd.Timestamp):
        dt = ts.to_pydatetime()
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return pd.Timestamp(ts).to_pydatetime().replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Public strategy function
# ---------------------------------------------------------------------------

def SMAStrategy(
    df: pd.DataFrame,
    ticker: str = "UNKNOWN",
    config: StrategyConfig | None = None,
) -> SignalResult:
    """Enhanced SMA crossover strategy with volume confirmation + trend filter.

    Parameters
    ----------
    df     : DataFrame with at minimum 'price' and 'timestamp' columns.
             Optional: 'volume', 'open', 'high', 'low', 'close'.
    ticker : Ticker symbol (propagated into the returned SignalResult).
    config : StrategyConfig; uses package defaults when omitted.

    Returns
    -------
    SignalResult
    """
    cfg  = config or _DEFAULT_CONFIG
    name = "SMAStrategy"

    # ── Guard: required columns ───────────────────────────────────────────────
    required = {"timestamp", "price"}
    missing  = required - set(df.columns)
    if missing:
        return SignalResult(
            ticker=ticker,
            signal="HOLD",
            confidence=0.0,
            reason=f"Missing required columns: {missing}",
            strategy_name=name,
            timestamp=datetime.now(tz=timezone.utc),
        )

    # ── Guard: minimum rows ───────────────────────────────────────────────────
    min_rows = max(cfg.sma_trend_window, cfg.sma_long_window) + 1
    if len(df) < min_rows:
        return SignalResult(
            ticker=ticker,
            signal="HOLD",
            confidence=0.0,
            reason=(
                f"Not enough data: need >={min_rows} bars, got {len(df)}"
            ),
            strategy_name=name,
            timestamp=_last_timestamp(df),
        )

    prices: pd.Series = df["price"].reset_index(drop=True)
    ts = _last_timestamp(df)

    # ── Moving averages ───────────────────────────────────────────────────────
    short_ma = _rolling_mean(prices, cfg.sma_short_window)
    long_ma  = _rolling_mean(prices, cfg.sma_long_window)
    trend_ma = _rolling_mean(prices, cfg.sma_trend_window)

    short_now  = short_ma.iloc[-1]
    short_prev = short_ma.iloc[-2]
    long_now   = long_ma.iloc[-1]
    long_prev  = long_ma.iloc[-2]
    trend_now  = trend_ma.iloc[-1]
    price_now  = prices.iloc[-1]

    if any(pd.isna(v) for v in [short_now, short_prev, long_now, long_prev, trend_now]):
        return SignalResult(
            ticker=ticker,
            signal="HOLD",
            confidence=0.0,
            reason="One or more MAs contain NaN — not enough seeding data yet.",
            strategy_name=name,
            timestamp=ts,
        )

    # ── Crossover detection ───────────────────────────────────────────────────
    crossed_up   = (short_prev <= long_prev) and (short_now > long_now)
    crossed_down = (short_prev >= long_prev) and (short_now < long_now)

    # ── Volume confirmation ───────────────────────────────────────────────────
    has_volume       = "volume" in df.columns
    volume_confirmed = True
    volume_detail    = "no volume data — confirmation skipped"
    if has_volume:
        vol: pd.Series = df["volume"].reset_index(drop=True)
        vol_ma = _rolling_mean(vol, cfg.sma_volume_window)
        vol_now    = vol.iloc[-1]
        vol_ma_now = vol_ma.iloc[-1]
        if not pd.isna(vol_ma_now) and vol_ma_now > 0:
            volume_confirmed = bool(vol_now > vol_ma_now)
            ratio = vol_now / vol_ma_now
            volume_detail = (
                f"vol={vol_now:,.0f} vs {cfg.sma_volume_window}d-avg={vol_ma_now:,.0f} "
                f"(x{ratio:.2f})"
            )
        else:
            volume_detail = "volume MA not yet seeded"

    # ── Trend filter ──────────────────────────────────────────────────────────
    above_trend  = bool(price_now > trend_now)
    trend_detail = (
        f"price={price_now:.2f} {'>' if above_trend else '<'} "
        f"{cfg.sma_trend_window}d-MA={trend_now:.2f}"
    )

    # ── Confidence ────────────────────────────────────────────────────────────
    if long_now != 0:
        distance_pct = abs((short_now - long_now) / long_now) * 100.0
    else:
        distance_pct = 0.0
    confidence = float(np.clip(distance_pct / 2.0, 0.0, 1.0))

    # ── BUY signal ───────────────────────────────────────────────────────────
    if crossed_up and volume_confirmed and above_trend:
        return SignalResult(
            ticker=ticker,
            signal="BUY",
            confidence=confidence,
            reason=(
                f"Bullish crossover: Short-MA({cfg.sma_short_window})={short_now:.2f} "
                f"> Long-MA({cfg.sma_long_window})={long_now:.2f}. "
                f"{trend_detail}. {volume_detail}."
            ),
            strategy_name=name,
            timestamp=ts,
        )

    # ── SELL signal ───────────────────────────────────────────────────────────
    if crossed_down and volume_confirmed:
        return SignalResult(
            ticker=ticker,
            signal="SELL",
            confidence=confidence,
            reason=(
                f"Bearish crossover: Short-MA({cfg.sma_short_window})={short_now:.2f} "
                f"< Long-MA({cfg.sma_long_window})={long_now:.2f}. "
                f"{trend_detail}. {volume_detail}."
            ),
            strategy_name=name,
            timestamp=ts,
        )

    # ── HOLD with informative reason ──────────────────────────────────────────
    if crossed_up and not above_trend:
        hold_reason = (
            f"Bullish crossover blocked by trend filter — "
            f"price below {cfg.sma_trend_window}d-MA. {trend_detail}."
        )
    elif crossed_up and not volume_confirmed:
        hold_reason = (
            f"Bullish crossover blocked — insufficient volume. {volume_detail}."
        )
    elif crossed_down and not volume_confirmed:
        hold_reason = (
            f"Bearish crossover blocked — insufficient volume. {volume_detail}."
        )
    else:
        direction = "above" if short_now > long_now else "below"
        hold_reason = (
            f"No crossover. Short-MA({cfg.sma_short_window})={short_now:.2f} "
            f"is {direction} Long-MA({cfg.sma_long_window})={long_now:.2f}. "
            f"Gap={distance_pct:.2f}%."
        )

    return SignalResult(
        ticker=ticker,
        signal="HOLD",
        confidence=0.0,
        reason=hold_reason,
        strategy_name=name,
        timestamp=ts,
    )


# ---------------------------------------------------------------------------
# Class wrapper — allows  SMAStrategy(config).generate(ticker, df)
# so the ensemble and backtest can use a uniform interface.
# ---------------------------------------------------------------------------

# Keep a reference to the original function before we reassign the name
_sma_fn = SMAStrategy


class SMAStrategy:  # type: ignore[no-redef]
    """Class wrapper around the SMAStrategy function for uniform .generate() API."""

    def __init__(self, config: StrategyConfig | None = None) -> None:
        self.config = config or _DEFAULT_CONFIG

    def generate(self, ticker: str, history_df: pd.DataFrame) -> SignalResult:
        df = history_df
        # Ensure 'price' column exists
        if "price" not in df.columns:
            for src in ("close", "Close"):
                if src in df.columns:
                    df = df.copy()
                    df["price"] = df[src]
                    break
        # Ensure 'timestamp' column exists
        if "timestamp" not in df.columns:
            df = df.copy()
            df["timestamp"] = range(len(df))
        return _sma_fn(df, ticker=ticker, config=self.config)