"""
forex/strategy_donchian.py
--------------------------
FX Strategy 3 — Donchian Channel Breakout (strict mode).

CONCEPT:
  A 30-day high/low channel breakout with mandatory trend + momentum filters.
  Fires only when EMA(200) trend AND ADX(14) ≥ 25 both confirm the breakout.
  This eliminates counter-trend entries and choppy-range false breakouts.

ENTRY (all three must be true):
  Long : close > 30-day highest close  AND  close > EMA(200)  AND  ADX ≥ 25
  Short: close < 30-day lowest close   AND  close < EMA(200)  AND  ADX ≥ 25

EXIT (first hit):
  A. Donchian trailing: 15-day lowest close (long) / highest close (short)
  B. ATR hard stop: 2.0 × ATR(14)
  C. Time stop: 30 calendar days

SIZING: 1% equity risk per trade, ATR-based.

THIS MODULE IS PURE — no I/O, no orders, no state.
"""

import numpy as np
import pandas as pd

BREAKOUT_PERIOD = 30      # wider channel = fewer but cleaner breakouts
EXIT_PERIOD     = 15      # proportional exit channel
ATR_PERIOD      = 14
EMA_TREND       = 200     # macro trend filter
ADX_PERIOD      = 14
ADX_MIN         = 25      # require confirmed trend strength
ATR_STOP_MULT   = 2.0
RISK_PCT        = 0.01
MAX_POSITIONS   = 4
TIME_STOP_DAYS  = 30
LOT_ROUND       = 1_000
MIN_BARS        = EMA_TREND + ATR_PERIOD + 5


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    prev = closes.shift(1)
    tr   = pd.concat([
        highs - lows,
        (highs - prev).abs(),
        (lows  - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def _adx(h: pd.Series, l: pd.Series, c: pd.Series, period: int = ADX_PERIOD) -> pd.Series:
    prev_c = c.shift(1); prev_h = h.shift(1); prev_l = l.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    dm_plus  = ((h - prev_h).clip(lower=0)).where(h - prev_h > prev_l - l, 0)
    dm_minus = ((prev_l - l).clip(lower=0)).where(prev_l - l > h - prev_h, 0)
    atr14     = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
    di_plus   = 100 * dm_plus.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr14
    di_minus  = 100 * dm_minus.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr14
    dx        = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus + 1e-9)
    return dx.ewm(alpha=1/period, adjust=False, min_periods=period).mean()


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Return strict Donchian breakout signals: EMA(200) + ADX(25) both required."""
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
        high30   = float(history.max())
        low30    = float(history.min())
        atr_val  = float(_atr(h, l, c).iloc[-1])
        ema200   = float(c.ewm(span=EMA_TREND, adjust=False).mean().iloc[-1])
        adx_val  = float(_adx(h, l, c).iloc[-1])

        if np.isnan(high30) or np.isnan(low30) or atr_val <= 0:
            continue
        if np.isnan(ema200) or np.isnan(adx_val):
            continue
        if adx_val < ADX_MIN:          # trend not strong enough — skip
            continue

        if today > high30 and today > ema200:          # breakout WITH macro trend
            stop  = today - ATR_STOP_MULT * atr_val
            score = (today - high30) / atr_val
            signals.append({
                "symbol":         sym,
                "direction":      "Buy",
                "score":          float(score),
                "atr":            float(atr_val),
                "close":          today,
                "stop_price":     float(stop),
                "breakout_level": float(high30),
                "adx":            float(adx_val),
            })
        elif today < low30 and today < ema200:         # breakdown WITH macro trend
            stop  = today + ATR_STOP_MULT * atr_val
            score = (low30 - today) / atr_val
            signals.append({
                "symbol":         sym,
                "direction":      "Sell",
                "score":          float(score),
                "atr":            float(atr_val),
                "close":          today,
                "stop_price":     float(stop),
                "breakout_level": float(low30),
                "adx":            float(adx_val),
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
