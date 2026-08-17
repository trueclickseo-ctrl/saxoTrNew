"""
forex/strategy_donchian.py
--------------------------
FX Strategy 3 — Donchian Channel Breakout.

CONCEPT:
  A 20-day high/low channel breakout applied to FX pairs.
  When price closes above its 20-day high, the pair is breaking out to the upside.
  When price closes below its 20-day low, it's breaking down.
  All 7 pairs are fully bidirectional — FX trends in both directions equally well.

  No regime filter needed: unlike equity futures, FX pairs don't follow a
  single macro regime. EUR/USD and USD/JPY often move in opposite directions.

ENTRY:
  Long  (all pairs): close > 20-day highest close
  Short (all pairs): close < 20-day lowest close

EXIT (first hit):
  A. Donchian trailing: 10-day lowest close (long) / highest close (short)
  B. ATR hard stop: 2.0 × ATR(14)
  C. Time stop: 30 calendar days

SIZING: 1% equity risk per trade, ATR-based.

THIS MODULE IS PURE — no I/O, no orders, no state.
"""

import numpy as np
import pandas as pd

BREAKOUT_PERIOD = 20
EXIT_PERIOD     = 10
ATR_PERIOD      = 14
ATR_STOP_MULT   = 2.0
RISK_PCT        = 0.01
MAX_POSITIONS   = 4
TIME_STOP_DAYS  = 30
LOT_ROUND       = 1_000
MIN_BARS        = BREAKOUT_PERIOD + ATR_PERIOD + 5


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    prev = closes.shift(1)
    tr   = pd.concat([
        highs - lows,
        (highs - prev).abs(),
        (lows  - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Return Donchian breakout signals for all FX pairs (fully bidirectional)."""
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        today    = float(c.iloc[-1])
        history  = c.iloc[-(BREAKOUT_PERIOD + 1):-1]
        high20   = float(history.max())
        low20    = float(history.min())
        atr_val  = float(_atr(h, l, c).iloc[-1])

        if np.isnan(high20) or np.isnan(low20) or atr_val <= 0:
            continue

        if today > high20:
            stop  = today - ATR_STOP_MULT * atr_val
            score = (today - high20) / atr_val
            signals.append({
                "symbol":          sym,
                "direction":       "Buy",
                "score":           float(score),
                "atr":             float(atr_val),
                "close":           today,
                "stop_price":      float(stop),
                "breakout_level":  float(high20),
            })
        elif today < low20:
            stop  = today + ATR_STOP_MULT * atr_val
            score = (low20 - today) / atr_val
            signals.append({
                "symbol":          sym,
                "direction":       "Sell",
                "score":           float(score),
                "atr":             float(atr_val),
                "close":           today,
                "stop_price":      float(stop),
                "breakout_level":  float(low20),
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < EXIT_PERIOD + 2:
        return False, ""

    c         = df["Close"].dropna()
    h         = df["High"].dropna()
    l         = df["Low"].dropna()
    today     = float(c.iloc[-1])
    direction = position["direction"]
    stop_px   = position["stop_price"]

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    if direction == "Buy":
        if stop_px > 0 and float(l.iloc[-1]) <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        low10 = float(c.iloc[-(EXIT_PERIOD + 1):-1].min())
        if today <= low10:
            return True, f"donchian_exit ({EXIT_PERIOD}d low {low10:.5f})"
    else:
        if stop_px > 0 and float(h.iloc[-1]) >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        high10 = float(c.iloc[-(EXIT_PERIOD + 1):-1].max())
        if today >= high10:
            return True, f"donchian_exit ({EXIT_PERIOD}d high {high10:.5f})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  min_units: float = 1_000) -> int:
    risk_amount   = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return int(min_units)
    raw = risk_amount / stop_distance
    return max(int(min_units), int(raw / min_units) * int(min_units))


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    band = ATR_STOP_MULT * current_atr
    if direction == "Buy":
        return max(current_stop, current_price - band)
    return min(current_stop, current_price + band)


def scan_summary(market_data: dict) -> list:
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        today    = float(c.iloc[-1])
        history  = c.iloc[-(BREAKOUT_PERIOD + 1):-1]
        high20   = float(history.max())
        low20    = float(history.min())
        atr_val  = float(_atr(h, l, c).iloc[-1])
        gap_hi   = (today - high20) / atr_val if atr_val > 0 else 0
        gap_lo   = (low20 - today)  / atr_val if atr_val > 0 else 0
        if today > high20:
            signal = "BREAKOUT!"
        elif today < low20:
            signal = "BREAKDOWN!"
        else:
            pct_pos = (today - low20) / (high20 - low20) * 100 if high20 != low20 else 50
            signal  = f"range {pct_pos:.0f}%"
        rows.append({
            "symbol":  sym, "close": today,
            "high20":  high20, "low20": low20,
            "atr":     atr_val, "gap_hi": gap_hi, "gap_lo": gap_lo,
            "signal":  signal,  "status": "ok",
        })
    return rows
