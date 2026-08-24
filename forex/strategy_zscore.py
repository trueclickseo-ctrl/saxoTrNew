"""
forex/strategy_zscore.py
-------------------------
FX Strategy 9 — Z-Score Mean Reversion.

CONCEPT:
  When price deviates more than 2 standard deviations from its 20-day mean,
  it is statistically overextended. We fade the extreme and target reversion
  back to the mean. This is a more rigorous variant of BB reversion — uses
  the actual z-score (normalized distance in σ units) rather than a fixed band.

  Only trade against the minor move — must be within EMA(200) macro trend zone
  to avoid fading a genuine trend breakout.

ENTRY:
  Long : z-score < -2.0  AND  close > EMA(200) * 0.99  (not in extreme downtrend)
  Short: z-score > +2.0  AND  close < EMA(200) * 1.01  (not in extreme uptrend)

EXIT (first hit):
  A. Z-score reverts to ±0.3 (returned to mean)
  B. ATR hard stop: 2.5 × ATR(14)
  C. Time stop: 12 calendar days (short-cycle strategy)

SIZING: 1% equity risk per trade, ATR-based.
Win Rate: ~63%
"""

import numpy as np
import pandas as pd

LOOKBACK      = 20
Z_ENTRY       = 2.0        # z-score threshold to enter
Z_EXIT        = 0.3        # z-score to declare reversion done
EMA_TREND     = 200
ATR_PERIOD    = 14
ATR_STOP_MULT = 2.5
RISK_PCT      = 0.0025  # was 0.01->0.005 on 2026-08-22, cut again 2026-08-24 -- see strategy.py's RISK_PCT comment
TIME_STOP_DAYS = 12
LOT_ROUND     = 1_000
MIN_BARS      = EMA_TREND + LOOKBACK + 5


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _zscore(c: pd.Series, window: int = LOOKBACK) -> pd.Series:
    mu  = c.rolling(window).mean()
    sig = c.rolling(window).std(ddof=1)
    return (c - mu) / (sig + 1e-10)


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()
    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        z        = _zscore(c)
        z_now    = float(z.iloc[-1])
        atr_val  = float(_atr(h, l, c).iloc[-1])
        ema200   = float(c.ewm(span=EMA_TREND, adjust=False).mean().iloc[-1])
        today    = float(c.iloc[-1])

        if np.isnan(z_now) or np.isnan(atr_val) or atr_val <= 0:
            continue

        # Long: extreme oversold dip within macro uptrend zone
        if z_now < -Z_ENTRY and today > ema200 * 0.99:
            signals.append({
                "symbol":    sym, "direction": "Buy",
                "score":     abs(z_now),
                "atr":       atr_val, "close": today,
                "stop_price": today - ATR_STOP_MULT * atr_val,
                "zscore":    z_now,
            })
        # Short: extreme overbought spike within macro downtrend zone
        elif z_now > Z_ENTRY and today < ema200 * 1.01:
            signals.append({
                "symbol":    sym, "direction": "Sell",
                "score":     abs(z_now),
                "atr":       atr_val, "close": today,
                "stop_price": today + ATR_STOP_MULT * atr_val,
                "zscore":    z_now,
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < MIN_BARS:
        return False, ""
    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"
    h, l, c  = df["High"], df["Low"], df["Close"]
    z        = _zscore(c)
    z_now    = float(z.iloc[-1])
    stop_px  = position["stop_price"]
    is_long  = position["direction"] == "Buy"
    high_now = float(h.iloc[-1])
    low_now  = float(l.iloc[-1])
    if is_long:
        if stop_px > 0 and low_now <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if z_now >= -Z_EXIT:
            return True, f"zscore_reverted ({z_now:+.2f})"
    else:
        if stop_px > 0 and high_now >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if z_now <= Z_EXIT:
            return True, f"zscore_reverted ({z_now:+.2f})"
    return False, ""


def size_position(account_equity: float, atr: float, min_units: float = 1_000) -> int:
    risk_amount   = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return int(min_units)
    raw = risk_amount / stop_distance
    return max(int(min_units), int(raw / min_units) * int(min_units))


def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        z = _zscore(c)
        rows.append({
            "symbol": sym, "close": float(c.iloc[-1]),
            "zscore": float(z.iloc[-1]),
            "atr":    float(_atr(h, l, c).iloc[-1]), "status": "ok",
        })
    return rows
