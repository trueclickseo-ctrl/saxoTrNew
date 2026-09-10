"""
forex/strategy_nzd_reversal.py
-------------------------------
FX Strategy — NZD Contrarian (AI-derived, 2026-09-08).

INSIGHT (from pnl_ledger.db — 150+ NZD closed SIM trades across all strategies):

  NZD BUY positions lose across EVERY trend-following strategy:
    RSI Buy NZD:       22 trades,  27% WR, -380 EUR
    Pullback Buy NZD:  10 trades,  20% WR, -719 EUR
    Gap Buy NZD:       16 trades,  38% WR, -1827 EUR
    EMA Sell NZD:      41 trades,   2% WR, -369 EUR  (short NZD in ema = also bad)

  NZD SELL positions (short NZD) outperform strongly:
    Gap Sell NZD:      18 trades,  39% WR, +16,712 EUR  (asymmetric winners)
    BB Sell NZD:        3 trades, 100% WR,    +34 EUR
    ZScore Sell NZD:    2 trades, 100% WR,    +29 EUR

STRATEGY:
  Use RSI(2) entry logic but REVERSE the direction on NZD-containing pairs.
  When RSI says "Buy NZD" (oversold in uptrend) → we Sell NZD instead.
  When RSI says "Sell NZD" (overbought in downtrend) → we Buy NZD instead.

  Rationale: NZD tends to exhibit persistent weakness even when RSI indicates
  a bounce should occur. The "oversold bounce" signal becomes a short entry
  because NZD's oversold conditions continue to deepen rather than recover.
  This is a structurally contrarian view specific to NZD pairs.

ENTRY (RSI(2) inverted):
  Sell NZD: close > EMA(200) AND RSI(2) < 10
    (original RSI would Buy here; we Sell expecting continued weakness)
  Buy NZD:  close < EMA(200) AND RSI(2) > 90
    (original RSI would Sell here; we Buy expecting the squeeze to end)

EXIT (same RSI recovery thresholds — mirrored for the reversed position):
  A. RSI recovery: RSI(2) >= 55 (short exit) or <= 45 (long exit)
  B. Hard stop: 1.5 × ATR from entry (mirrored)
  C. Time stop: 12 calendar days

SIM-ONLY research track. Never in LIVE_ALLOWED_STRATEGIES.
Needs ≥30 closed trades before the AI evolver considers writing an override.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

RSI_PERIOD     = 2
RSI_OVERSOLD   = 10
RSI_OVERBOUGHT = 90
RSI_EXIT_LONG  = 45   # exit a reversed-Buy (was original Sell) when RSI drops here
RSI_EXIT_SHORT = 55   # exit a reversed-Sell (was original Buy) when RSI rises here
TREND_EMA      = 200
ATR_PERIOD     = 14
ATR_STOP_MULT  = 1.5
RISK_PCT       = 0.0025
MAX_POSITIONS  = 20
TIME_STOP_DAYS  = 12
PROFIT_TARGET_R = 1.0
LOT_ROUND      = 1_000
MIN_BARS       = TREND_EMA + RSI_PERIOD + 5


def _rsi(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = closes.diff()
    gain  = delta.where(delta > 0, 0.0).ewm(alpha=1.0 / period, adjust=False,
                                             min_periods=period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0 / period, adjust=False,
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
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Return contrarian NZD signals — RSI(2) logic with direction reversed.

    Only fires on pairs containing 'NZD'. Returns signals sorted by RSI
    extremity (most extreme first).
    """
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for sym, df in market_data.items():
        if "NZD" not in sym.upper():
            continue
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        rsi_s   = _rsi(c)
        ema200  = _ema(c, TREND_EMA)
        atr_s   = _atr(h, l, c)

        cur_rsi   = float(rsi_s.iloc[-1])
        cur_ema   = float(ema200.iloc[-1])
        cur_close = float(c.iloc[-1])
        cur_atr   = float(atr_s.iloc[-1])

        if np.isnan(cur_rsi) or np.isnan(cur_ema) or cur_atr <= 0:
            continue

        in_uptrend   = cur_close > cur_ema
        in_downtrend = cur_close < cur_ema

        # Original RSI says Buy → we SELL (reversed)
        if in_uptrend and cur_rsi <= RSI_OVERSOLD:
            stop  = cur_close + ATR_STOP_MULT * cur_atr   # stop above (short)
            score = RSI_OVERSOLD - cur_rsi
            signals.append({
                "symbol":     sym,
                "direction":  "Sell",   # reversed from RSI's Buy
                "score":      float(score),
                "rsi":        cur_rsi,
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
                "reversal":   True,
            })

        # Original RSI says Sell → we BUY (reversed)
        elif in_downtrend and cur_rsi >= RSI_OVERBOUGHT:
            stop  = cur_close - ATR_STOP_MULT * cur_atr   # stop below (long)
            score = cur_rsi - RSI_OVERBOUGHT
            signals.append({
                "symbol":     sym,
                "direction":  "Buy",    # reversed from RSI's Sell
                "score":      float(score),
                "rsi":        cur_rsi,
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
                "reversal":   True,
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Exit when RSI recovers for the reversed position, hard-stop, or time-stop."""
    if df is None or len(df) < RSI_PERIOD + 2:
        return False, ""

    h, l, c  = df["High"], df["Low"], df["Close"]
    cur_rsi  = float(_rsi(c).iloc[-1])
    cur_high = float(h.iloc[-1])
    cur_low  = float(l.iloc[-1])
    direction = position["direction"]
    stop_px   = position["stop_price"]

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    entry        = float(position.get("entry_price", 0))
    initial_stop = float(position.get("initial_stop_price") or 0)
    R = abs(entry - initial_stop)
    if R > 0:
        cur_close = float(c.iloc[-1])
        profit = (cur_close - entry) if direction == "Buy" else (entry - cur_close)
        if profit / R >= PROFIT_TARGET_R:
            return True, f"profit_target ({profit / R:.2f}R >= {PROFIT_TARGET_R}R)"

    if direction == "Sell":
        # Reversed-Sell: exit when RSI rises back to 55 (short squeeze ending)
        if cur_rsi >= RSI_EXIT_SHORT:
            return True, f"rsi_recovery ({cur_rsi:.1f}>={RSI_EXIT_SHORT})"
        if cur_high >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
    else:
        # Reversed-Buy: exit when RSI falls back to 45 (long momentum fading)
        if cur_rsi <= RSI_EXIT_LONG:
            return True, f"rsi_recovery ({cur_rsi:.1f}<={RSI_EXIT_LONG})"
        if cur_low <= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  min_units: int = LOT_ROUND, risk_pct: float | None = None,
                  block_below_min: bool = False) -> int:
    """Risk RISK_PCT of equity per trade, ATR-based stop."""
    rp = risk_pct if risk_pct is not None else RISK_PCT
    if atr <= 0 or account_equity <= 0:
        return 0
    risk_amount = account_equity * rp
    units = risk_amount / atr
    units = round(units / min_units) * min_units
    if block_below_min and units < min_units:
        return 0
    return max(0, int(units))


def scan_summary(market_data: dict) -> list:
    """Return a scan summary row for each NZD pair (for the --scan panel)."""
    rows = []
    for sym, df in market_data.items():
        if "NZD" not in sym.upper() or df is None or len(df) < MIN_BARS:
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        cur_rsi   = float(_rsi(c).iloc[-1])
        cur_ema   = float(_ema(c, TREND_EMA).iloc[-1])
        cur_close = float(c.iloc[-1])
        trend     = "UP" if cur_close > cur_ema else "DN"
        rows.append({"symbol": sym, "close": cur_close, "rsi": cur_rsi, "trend": trend})
    return sorted(rows, key=lambda x: x["rsi"])
