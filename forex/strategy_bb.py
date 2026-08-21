"""
forex/strategy_bb.py
---------------------
FX Strategy 4 — Bollinger Band Mean Reversion.

CONCEPT:
  Fade extreme price moves using Bollinger Bands (20,2) + RSI(14) confirmation.
  Price touching the upper/lower band is a 2-sigma excursion — statistically
  it reverts to the mean (BB mid = 20-day SMA) within a few sessions.

  Combined with RSI(14) overbought/oversold confirmation this filters out
  trending breakouts where the band walk continues.

ENTRY:
  SHORT: close > BB_upper  AND  RSI(14) > 65   (overbought excursion)
  LONG:  close < BB_lower  AND  RSI(14) < 35   (oversold excursion)
  Bidirectional across all 12 pairs.

EXIT (first hit):
  A. BB mid reversion: close crosses back through 20-day SMA
  B. ATR hard stop:    2.0 × ATR(14) from entry
  C. Time stop:        8 calendar days (short-term strategy)

SIZING: 1% equity risk per trade, ATR-based.

THIS MODULE IS PURE — no I/O, no orders, no state.
"""

import numpy as np
import pandas as pd

BB_PERIOD      = 20
BB_STD         = 2.0
RSI_PERIOD     = 14
RSI_OB         = 65    # overbought → short entry
RSI_OS         = 35    # oversold  → long entry
ATR_PERIOD     = 14
ATR_STOP_MULT  = 2.0
RISK_PCT       = 0.005  # was 0.01, cut 2026-08-22 to free shared margin for more pairs
MAX_POSITIONS  = 4
TIME_STOP_DAYS = 8
LOT_ROUND      = 1_000
MIN_BARS       = BB_PERIOD + RSI_PERIOD + 5


def _bb(closes: pd.Series) -> tuple:
    """Return (upper, mid, lower) Bollinger Band series."""
    mid   = closes.rolling(BB_PERIOD).mean()
    std   = closes.rolling(BB_PERIOD).std(ddof=0)
    return mid + BB_STD * std, mid, mid - BB_STD * std


def _rsi(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = closes.diff()
    gain  = delta.where(delta > 0, 0.0).ewm(alpha=1.0/period, adjust=False,
                                             min_periods=period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0/period, adjust=False,
                                                min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Return BB mean-reversion signals. Strongest excursions first."""
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        h, l, c    = df["High"], df["Low"], df["Close"]
        bb_u, bb_m, bb_l = _bb(c)
        rsi_s      = _rsi(c)
        atr_s      = _atr(h, l, c)

        cur_close  = float(c.iloc[-1])
        cur_bbu    = float(bb_u.iloc[-1])
        cur_bbm    = float(bb_m.iloc[-1])
        cur_bbl    = float(bb_l.iloc[-1])
        cur_rsi    = float(rsi_s.iloc[-1])
        cur_atr    = float(atr_s.iloc[-1])

        if any(np.isnan(v) for v in (cur_bbu, cur_bbm, cur_bbl, cur_rsi, cur_atr)):
            continue
        if cur_atr <= 0:
            continue

        if cur_close > cur_bbu and cur_rsi > RSI_OB:
            # Overbought excursion → SHORT; target: BB mid
            stop  = cur_close + ATR_STOP_MULT * cur_atr
            score = (cur_close - cur_bbu) / cur_atr  # distance above band in ATR units
            signals.append({
                "symbol":     sym,
                "direction":  "Sell",
                "score":      float(score),
                "rsi":        cur_rsi,
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
                "bb_target":  float(cur_bbm),
            })

        elif cur_close < cur_bbl and cur_rsi < RSI_OS:
            # Oversold excursion → LONG; target: BB mid
            stop  = cur_close - ATR_STOP_MULT * cur_atr
            score = (cur_bbl - cur_close) / cur_atr
            signals.append({
                "symbol":     sym,
                "direction":  "Buy",
                "score":      float(score),
                "rsi":        cur_rsi,
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
                "bb_target":  float(cur_bbm),
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < BB_PERIOD + 2:
        return False, ""

    h, l, c       = df["High"], df["Low"], df["Close"]
    _, bb_m, _    = _bb(c)
    cur_close     = float(c.iloc[-1])
    cur_mid       = float(bb_m.iloc[-1])
    cur_high      = float(h.iloc[-1])
    cur_low       = float(l.iloc[-1])
    direction     = position["direction"]
    stop_px       = position["stop_price"]

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    if direction == "Buy":
        if cur_close >= cur_mid:
            return True, f"bb_mid_reversion ({cur_close:.5f}>={cur_mid:.5f})"
        if cur_low <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
    else:  # Sell / short
        if cur_close <= cur_mid:
            return True, f"bb_mid_reversion ({cur_close:.5f}<={cur_mid:.5f})"
        if cur_high >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"

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
        h, l, c          = df["High"], df["Low"], df["Close"]
        bb_u, bb_m, bb_l = _bb(c)
        cur_rsi          = float(_rsi(c).iloc[-1])
        cur_close        = float(c.iloc[-1])
        bbu              = float(bb_u.iloc[-1])
        bbm              = float(bb_m.iloc[-1])
        bbl              = float(bb_l.iloc[-1])
        bb_pct           = (cur_close - bbl) / (bbu - bbl) * 100 if (bbu - bbl) > 0 else 50
        flag = ""
        if cur_close > bbu and cur_rsi > RSI_OB:
            flag = "OB-SHORT"
        elif cur_close < bbl and cur_rsi < RSI_OS:
            flag = "OS-LONG"
        rows.append({
            "symbol": sym, "close": cur_close,
            "rsi14":  cur_rsi, "bb_pct": bb_pct,
            "bb_upper": bbu, "bb_mid": bbm, "bb_lower": bbl,
            "flag":   flag, "status": "ok",
        })
    return rows
