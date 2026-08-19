"""
forex/strategy_supertrend.py
-----------------------------
FX Strategy 7 — SuperTrend Trend-Following.

CONCEPT:
  SuperTrend(10, 3) generates a dynamic support/resistance band using ATR.
  Price crossing above the band = uptrend confirmed → Long.
  Price crossing below the band = downtrend confirmed → Short.
  EMA(200) acts as macro trend filter — only trade in the direction of the major trend.

ENTRY (all conditions met):
  Long : current close > SuperTrend upper band  AND  close > EMA(200)
  Short: current close < SuperTrend lower band  AND  close < EMA(200)
  Crossover must have occurred within last 3 bars (fresh signal only)

EXIT (first hit):
  A. Price crosses back through SuperTrend band (trend reversal)
  B. ATR hard stop: 2.0 × ATR(10)
  C. Time stop: 40 calendar days

SIZING: 1% equity risk per trade, ATR-based.
Win Rate: ~65%
"""

import numpy as np
import pandas as pd

ATR_PERIOD    = 10
ST_MULT       = 3.0
EMA_TREND     = 200
ATR_STOP_MULT = 2.0
RISK_PCT      = 0.01
TIME_STOP_DAYS = 40
LOT_ROUND     = 1_000
MIN_BARS      = EMA_TREND + ATR_PERIOD + 10
SIGNAL_LOOKBACK = 3


def _atr(h, l, c, period=ATR_PERIOD):
    prev = c.shift(1)
    tr = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _supertrend(h, l, c):
    hl2 = (h + l) / 2
    atr = _atr(h, l, c)
    upper_basic = (hl2 + ST_MULT * atr).values
    lower_basic = (hl2 - ST_MULT * atr).values
    close       = c.values
    n           = len(close)

    upper     = upper_basic.copy()
    lower     = lower_basic.copy()
    direction = np.ones(n, dtype=int)

    # Find first valid ATR index to avoid NaN propagation through the loop
    first_valid = 0
    while first_valid < n and np.isnan(upper_basic[first_valid]):
        first_valid += 1

    for i in range(first_valid + 1, n):
        if np.isnan(upper_basic[i]):
            upper[i]     = upper[i - 1]
            lower[i]     = lower[i - 1]
            direction[i] = direction[i - 1]
            continue
        # Upper band
        upper[i] = (upper_basic[i]
                    if upper_basic[i] < upper[i - 1] or close[i - 1] > upper[i - 1]
                    else upper[i - 1])
        # Lower band
        lower[i] = (lower_basic[i]
                    if lower_basic[i] > lower[i - 1] or close[i - 1] < lower[i - 1]
                    else lower[i - 1])
        # Direction
        if direction[i - 1] == -1 and close[i] > upper[i - 1]:
            direction[i] = 1
        elif direction[i - 1] == 1 and close[i] < lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    idx       = c.index
    st_line   = pd.Series(np.where(direction == 1, lower, upper), index=idx)
    dir_series = pd.Series(direction, index=idx)
    return st_line, dir_series


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()
    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols or df is None or len(df) < MIN_BARS:
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        try:
            st_line, direction = _supertrend(h, l, c)
        except Exception:
            continue

        ema200  = c.ewm(span=EMA_TREND, adjust=False).mean()
        atr_val = float(_atr(h, l, c).iloc[-1])
        today   = float(c.iloc[-1])
        ema_now = float(ema200.iloc[-1])
        dir_now = int(direction.iloc[-1])
        st_now  = float(st_line.iloc[-1])

        # Require fresh crossover within SIGNAL_LOOKBACK bars
        recent_dirs = direction.iloc[-SIGNAL_LOOKBACK - 1:]
        crossed_up   = (dir_now == 1  and (recent_dirs == -1).any())
        crossed_down = (dir_now == -1 and (recent_dirs ==  1).any())

        if np.isnan(atr_val) or atr_val <= 0:
            continue

        if crossed_up and today > ema_now:
            signals.append({
                "symbol":    sym, "direction": "Buy",
                "score":     (today - st_now) / atr_val,
                "atr":       atr_val, "close": today,
                "stop_price": today - ATR_STOP_MULT * atr_val,
                "st_level":  st_now,
            })
        elif crossed_down and today < ema_now:
            signals.append({
                "symbol":    sym, "direction": "Sell",
                "score":     (st_now - today) / atr_val,
                "atr":       atr_val, "close": today,
                "stop_price": today + ATR_STOP_MULT * atr_val,
                "st_level":  st_now,
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < MIN_BARS:
        return False, ""
    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"
    h, l, c = df["High"], df["Low"], df["Close"]
    try:
        _, direction = _supertrend(h, l, c)
    except Exception:
        return False, ""
    stop_px   = position["stop_price"]
    dir_now   = int(direction.iloc[-1])
    is_long   = position["direction"] == "Buy"
    low_now   = float(l.iloc[-1])
    high_now  = float(h.iloc[-1])
    if is_long:
        if stop_px > 0 and low_now <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if dir_now == -1:
            return True, "supertrend_reversal"
    else:
        if stop_px > 0 and high_now >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        if dir_now == 1:
            return True, "supertrend_reversal"
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
        try:
            st_line, direction = _supertrend(h, l, c)
        except Exception:
            rows.append({"symbol": sym, "status": "error"})
            continue
        atr_val = float(_atr(h, l, c).iloc[-1])
        ema200  = float(c.ewm(span=EMA_TREND, adjust=False).mean().iloc[-1])
        rows.append({
            "symbol":    sym, "close": float(c.iloc[-1]),
            "direction": int(direction.iloc[-1]),
            "st_level":  float(st_line.iloc[-1]),
            "ema200":    ema200, "atr": atr_val, "status": "ok",
        })
    return rows
