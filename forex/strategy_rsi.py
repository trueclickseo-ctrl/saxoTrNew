"""
forex/strategy_rsi.py
---------------------
FX Strategy 2 — RSI(2) Pullback within Trend.

CONCEPT:
  Use EMA(200) to define the major trend direction.
  When price pulls back sharply (RSI(2) < 10 in uptrend), buy the dip.
  When price spikes up sharply (RSI(2) > 90 in downtrend), sell the spike.
  Exit when RSI recovers to neutral (55 long / 45 short).

  This catches short-term exhaustion moves within established FX trends.
  RSI(2) oscillates daily on FX — a 2-day pullback against the trend
  typically snaps back within 3-8 sessions.

ENTRY:
  Long  (all pairs): close > EMA(200) AND RSI(2) < 10
  Short (all pairs): close < EMA(200) AND RSI(2) > 90
  All 7 pairs are bidirectional.

EXIT (first hit):
  A. RSI recovery: RSI(2) >= 55 (long) or <= 45 (short)
  B. ATR hard stop: 1.5 × ATR(14)
  C. Time stop: 12 calendar days

SIZING: 1% equity risk per trade, ATR-based.

THIS MODULE IS PURE — no I/O, no orders, no state.
"""

import numpy as np
import pandas as pd

RSI_PERIOD     = 2
RSI_OVERSOLD   = 10
RSI_OVERBOUGHT = 90
RSI_EXIT_LONG  = 55
RSI_EXIT_SHORT = 45
TREND_EMA      = 200
ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.5
RISK_PCT       = 0.0025  # was 0.01->0.005 on 2026-08-22, cut again 2026-08-24 -- see strategy.py's RISK_PCT comment
MAX_POSITIONS  = 4
TIME_STOP_DAYS = 12
LOT_ROUND      = 1_000
MIN_BARS       = TREND_EMA + RSI_PERIOD + 5


def _rsi(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = closes.diff()
    gain  = delta.where(delta > 0, 0.0).ewm(alpha=1.0/period, adjust=False,
                                             min_periods=period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0/period, adjust=False,
                                                min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Return RSI(2) pullback signals for all FX pairs.

    Returns list of signal dicts sorted by RSI distance (most extreme first).
    """
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        rsi_s    = _rsi(c)
        ema200   = _ema(c, TREND_EMA)
        atr_s    = _atr(h, l, c)

        cur_rsi   = float(rsi_s.iloc[-1])
        cur_ema   = float(ema200.iloc[-1])
        cur_close = float(c.iloc[-1])
        cur_atr   = float(atr_s.iloc[-1])

        if np.isnan(cur_rsi) or np.isnan(cur_ema) or cur_atr <= 0:
            continue

        in_uptrend   = cur_close > cur_ema
        in_downtrend = cur_close < cur_ema

        if in_uptrend and cur_rsi <= RSI_OVERSOLD:
            stop  = cur_close - ATR_STOP_MULT * cur_atr
            score = RSI_OVERSOLD - cur_rsi
            signals.append({
                "symbol":     sym,
                "direction":  "Buy",
                "score":      float(score),
                "rsi":        cur_rsi,
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
            })

        elif in_downtrend and cur_rsi >= RSI_OVERBOUGHT:
            stop  = cur_close + ATR_STOP_MULT * cur_atr
            score = cur_rsi - RSI_OVERBOUGHT
            signals.append({
                "symbol":     sym,
                "direction":  "Sell",
                "score":      float(score),
                "rsi":        cur_rsi,
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < RSI_PERIOD + 2:
        return False, ""

    h, l, c   = df["High"], df["Low"], df["Close"]
    cur_rsi   = float(_rsi(c).iloc[-1])
    cur_high  = float(h.iloc[-1])
    cur_low   = float(l.iloc[-1])
    direction = position["direction"]
    stop_px   = position["stop_price"]

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    if direction == "Buy":
        if cur_rsi >= RSI_EXIT_LONG:
            return True, f"rsi_recovery ({cur_rsi:.1f}>={RSI_EXIT_LONG})"
        if cur_low <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
    else:
        if cur_rsi <= RSI_EXIT_SHORT:
            return True, f"rsi_recovery ({cur_rsi:.1f}<={RSI_EXIT_SHORT})"
        if cur_high >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  min_units: float = 1_000, risk_pct: float | None = None) -> int:
    """risk_pct: override this module's own RISK_PCT (e.g. a smaller LIVE-
    pilot risk than SIM uses) -- defaults to RISK_PCT, so every existing
    caller (SIM) is unaffected. See forex/runner.py's _live_risk_pct()."""
    risk_amount   = account_equity * (risk_pct if risk_pct is not None else RISK_PCT)
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
        h, l, c   = df["High"], df["Low"], df["Close"]
        cur_rsi   = float(_rsi(c).iloc[-1])
        cur_ema   = float(_ema(c, TREND_EMA).iloc[-1])
        cur_close = float(c.iloc[-1])
        trend     = "BULL" if cur_close > cur_ema else "BEAR"
        flag      = "OS" if cur_rsi < RSI_OVERSOLD else ("OB" if cur_rsi > RSI_OVERBOUGHT else "  ")
        rows.append({
            "symbol": sym, "close": cur_close,
            "rsi2":   cur_rsi, "ema200": cur_ema,
            "trend":  trend,   "flag":   flag,
            "status": "ok",
        })
    return rows
