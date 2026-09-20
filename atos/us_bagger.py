"""
atos/us_bagger.py
------------------
US Momentum Bagger — multi-cap high-momentum scanner.

STATUS: SIM-ONLY. Paper trades only (paper=1 always). No real orders.

THESIS:
  Stocks that have already moved 80%+ in 6 months tend to continue.
  Trend persistence is the edge. We enter on consolidation near recent
  highs (not chasing parabolas), hold with a wide trailing stop, and
  let the trend run — no profit target, no fixed time exit.

ENTRY (all must hold):
  1. 6-month ROC > ROC_THRESHOLD (80%)       — already a bagger-in-progress
  2. Price within PULLBACK_FROM_HIGH (15%)    — not parabolic; room to continue
  3. Volume 20d avg > VOLUME_TREND_MIN (1.2x) of 60d avg — accumulation trend
  4. RSI(14) between RSI_MIN (40) and RSI_MAX (75) — not overbought/oversold
  5. Price > SMA50                            — medium-term trend intact

EXIT (first condition hit):
  A. Trailing stop: current_price < trailing_high * (1 - TRAILING_STOP_PCT)
  B. Overbought exhaustion: RSI(14) > 80 AND price < prior-3d high
  C. Emergency time stop: MAX_HOLD_DAYS (60) days

SIZING:
  MAX_POSITIONS = 5 concurrent bagger slots
  Sleeve: BAGGER_SLEEVE_SEK (defined in runner)

This module is PURE (no I/O, no orders):
  - scan(ohlcv_data, tickers)                      -> ranked candidates
  - should_exit(trade, current_price, trailing_high, rsi) -> (exit, reason)
The runner updates trailing_stop_high in the DB each cycle and calls should_exit.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ── Strategy parameters ────────────────────────────────────────────────────────
ROC_LOOKBACK        = 126   # trading days ≈ 6 calendar months
ROC_THRESHOLD       = 80.0  # % — must have moved at least 80% in 6 months
HIGH_LOOKBACK       = 252   # 52-week high lookback
PULLBACK_FROM_HIGH  = 0.15  # price must be within 15% of 52-week high
VOLUME_TREND_DAYS   = 20    # recent avg volume window
VOLUME_BASE_DAYS    = 60    # baseline avg volume window
VOLUME_TREND_MIN    = 1.20  # 20d avg > 1.2x 60d avg (accumulation signal)
RSI_PERIOD          = 14
RSI_MIN             = 40    # not deeply oversold
RSI_MAX             = 75    # not overbought at entry
SMA_PERIOD          = 50    # medium-term trend filter
TRAILING_STOP_PCT   = 0.12  # 12% below trailing high
RSI_EXIT_THRESHOLD  = 80    # overbought exhaustion exit trigger
MAX_HOLD_DAYS       = 60    # emergency time stop only
MAX_POSITIONS       = 5     # max concurrent bagger positions
# ─────────────────────────────────────────────────────────────────────────────


def _rsi(series: pd.Series, n: int) -> pd.Series:
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def scan(ohlcv_data: dict, tickers: list) -> list:
    """Scan BAGGER_TICKERS for momentum continuation entry signals.

    Returns candidates ranked by ROC (highest first):
      {ticker, price, roc_6m, rsi, sma50, pct_from_high, vol_trend, score}
    """
    min_bars = max(ROC_LOOKBACK, HIGH_LOOKBACK, VOLUME_BASE_DAYS, SMA_PERIOD) + 10
    candidates = []

    for ticker in tickers:
        df = ohlcv_data.get(ticker)
        if df is None or "Close" not in df or "Volume" not in df:
            continue

        close  = df["Close"].dropna().astype(float)
        volume = df["Volume"].dropna().astype(float)

        if len(close) < min_bars:
            continue

        price = float(close.iloc[-1])
        if price <= 0:
            continue

        # 1. 6-month ROC
        idx_6m = min(ROC_LOOKBACK, len(close) - 1)
        price_6m_ago = float(close.iloc[-(idx_6m + 1)])
        if price_6m_ago <= 0:
            continue
        roc_6m = (price - price_6m_ago) / price_6m_ago * 100
        if roc_6m < ROC_THRESHOLD:
            continue

        # 2. Not parabolic — within PULLBACK_FROM_HIGH of 52-week high
        lookback = min(HIGH_LOOKBACK, len(close))
        high_52w = float(close.iloc[-lookback:].max())
        pct_from_high = (high_52w - price) / high_52w
        if pct_from_high > PULLBACK_FROM_HIGH:
            continue

        # 3. Volume accumulation trend
        vol_20d = float(volume.rolling(VOLUME_TREND_DAYS).mean().iloc[-1])
        vol_60d = float(volume.rolling(VOLUME_BASE_DAYS).mean().iloc[-1])
        if pd.isna(vol_20d) or pd.isna(vol_60d) or vol_60d <= 0:
            continue
        vol_trend = vol_20d / vol_60d
        if vol_trend < VOLUME_TREND_MIN:
            continue

        # 4. RSI in range
        rsi_series = _rsi(close, RSI_PERIOD)
        rsi_val = float(rsi_series.iloc[-1])
        if pd.isna(rsi_val) or rsi_val < RSI_MIN or rsi_val > RSI_MAX:
            continue

        # 5. Price above SMA50
        sma50 = float(_sma(close, SMA_PERIOD).iloc[-1])
        if pd.isna(sma50) or price < sma50:
            continue

        # Score: higher ROC + closer to 52w high = stronger conviction
        score = roc_6m * (1 - pct_from_high) * vol_trend

        candidates.append({
            "ticker":        ticker,
            "price":         round(price, 4),
            "roc_6m":        round(roc_6m, 1),
            "rsi":           round(rsi_val, 1),
            "sma50":         round(sma50, 4),
            "pct_from_high": round(pct_from_high * 100, 1),
            "vol_trend":     round(vol_trend, 2),
            "score":         round(score, 2),
        })

    return sorted(candidates, key=lambda x: x["score"], reverse=True)


def compute_trailing_high(trade: dict, current_price: float) -> float:
    """Return the new trailing high — max of stored value and current price."""
    stored = float(trade.get("trailing_stop_high") or trade.get("entry_price") or 0)
    return max(stored, current_price)


def should_exit(
    trade: dict,
    current_price: float,
    trailing_high: float,
    close_series: "pd.Series | None" = None,
) -> tuple[bool, str]:
    """Check exit conditions for an open bagger position.

    Args:
        trade:          trade dict with at least 'entry_price', 'entry_date'
        current_price:  latest close price
        trailing_high:  highest close since entry (caller keeps this updated)
        close_series:   recent Close series for RSI computation (optional)

    Returns:
        (should_exit, reason)
    """
    from datetime import date

    # A. Trailing stop
    trail_stop = trailing_high * (1 - TRAILING_STOP_PCT)
    if trailing_high > 0 and current_price <= trail_stop:
        return True, (f"trailing stop: price {current_price:.2f} <= "
                      f"{trail_stop:.2f} (high {trailing_high:.2f} - {TRAILING_STOP_PCT*100:.0f}%)")

    # B. Overbought exhaustion: RSI > 80 AND price fell below 3-day high
    if close_series is not None and len(close_series) >= 5:
        try:
            rsi_now = float(_rsi(close_series, RSI_PERIOD).iloc[-1])
            high_3d = float(close_series.iloc[-4:-1].max())
            if rsi_now > RSI_EXIT_THRESHOLD and current_price < high_3d:
                return True, (f"overbought exhaustion: RSI={rsi_now:.0f} > {RSI_EXIT_THRESHOLD}, "
                              f"price {current_price:.2f} < 3d-high {high_3d:.2f}")
        except Exception:
            pass

    # C. Emergency time stop
    entry_date_str = trade.get("entry_date", "")
    if entry_date_str:
        try:
            held = (date.today() - date.fromisoformat(entry_date_str[:10])).days
            if held >= MAX_HOLD_DAYS:
                return True, f"time stop: {held}d >= {MAX_HOLD_DAYS}d max hold"
        except Exception:
            pass

    return False, ""
