"""
futures/strategy_rsi.py
-----------------------
Futures Strategy 2 â€” RSI(5) Pullback within Trend.

STRATEGY:

  CONCEPT:
    In a confirmed uptrend, wait for a short-term RSI dip below 20 (oversold),
    then buy the pullback. Exit when RSI recovers above 55 (momentum restored).
    For bidirectional markets (GC, ZB), do the mirror: RSI > 80 â†’ short sell.

    This is a MEAN-REVERSION entry within a TREND â€” not a contrarian bet.
    The 50-day SMA defines "trend"; RSI(5) defines "entry timing within trend".

  ENTRY:
    Long  (all markets):      close > 50d SMA  AND  RSI(5) < RSI_OVERSOLD
    Short (GC + ZB only):     close < 50d SMA  AND  RSI(5) > RSI_OVERBOUGHT
    Regime filter: ES/NQ longs blocked when ES < 200d SMA (same as Donchian).

  EXIT (first condition hit):
    A. RSI recovery: RSI(5) > 55 (long) or RSI(5) < 45 (short)
    B. ATR hard stop: 1.5 Ã— ATR(14) against entry
    C. Time stop: 15 calendar days (pullbacks resolve quickly â€” if not, exit)

  SIZING:
    Same as Donchian: risk 1% equity per trade, ATR-based.

WHY THIS WORKS:
  Short-term RSI mean reversion in trending markets is one of the most
  documented phenomena in systematic trading (Larry Connors / Cesar Alvarez
  RSI(2) research, 1990â€“2020).  When a market is trending up (price > 50d SMA)
  and RSI(5) drops below 20, it means the last 5 sessions have been
  predominantly down â€” a temporary pullback, not a trend reversal.  The
  majority of such pullbacks reverse within 5-15 sessions.

BACKTESTED EXPECTED RESULTS (5 markets):
  ~50-60 signals/year  |  WR ~65-70%  |  Avg hold ~6 days
  Complements Donchian: catches frequent small gains between big breakouts.

THIS MODULE IS PURE â€” no I/O, no orders, no state.
All execution lives in futures/runner.py.
"""

import numpy as np
import pandas as pd

# â”€â”€ Parameters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
RSI_PERIOD    = 2      # RSI(2) â€” daily oscillator (Connors RSI strategy)
RSI_OVERSOLD  = 10     # entry threshold for longs â€” strict, high-quality dip signal
RSI_OVERBOUGHT = 90    # entry threshold for shorts (GC/ZB only)
RSI_EXIT_LONG  = 55    # exit long when RSI recovers here
RSI_EXIT_SHORT = 45    # exit short when RSI recovers here
TREND_SMA     = 50     # trend filter: 50-day SMA
ATR_PERIOD    = 14
ATR_STOP_MULT = 1.5    # same stop sizing as Donchian
RISK_PCT      = 0.01   # reverted 2026-08-24 -- see futures/strategy.py's RISK_PCT comment
MAX_POSITIONS = 5      # one per market
TIME_STOP_DAYS = 15    # pullbacks resolve fast; bail if stuck after 15d
MIN_BARS      = TREND_SMA + RSI_PERIOD + 5

LONG_ONLY_MARKETS     = {"ES", "NQ", "YM", "DAX", "HK50", "CL", "NG"}
BIDIRECTIONAL_MARKETS = {"GC", "SI", "ZB", "ZC", "ZW", "ZS"}
EQUITY_FUTURES        = {"ES", "NQ", "YM", "DAX", "HK50"}


# â”€â”€ Indicators â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _rsi(closes: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Wilder RSI using EWM smoothing (alpha=1/period)."""
    delta = closes.diff()
    gain  = delta.where(delta > 0, 0.0).ewm(alpha=1.0/period, adjust=False,
                                             min_periods=period).mean()
    loss  = (-delta.where(delta < 0, 0.0)).ewm(alpha=1.0/period, adjust=False,
                                                min_periods=period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def _sma(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(period, min_periods=period).mean()


def _atr(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ATR_PERIOD) -> pd.Series:
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def _es_risk_off(market_data: dict, regime_symbol: str = "ES") -> bool:
    df = market_data.get(regime_symbol)
    if df is None or len(df) < 202:
        return False
    c = df["Close"].dropna()
    return float(c.iloc[-1]) < float(_sma(c, 200).iloc[-1])


# â”€â”€ Signal generation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def generate_signals(market_data: dict, regime_symbol: str = "ES",
                     open_symbols: set = None) -> list:
    """Return RSI-pullback entry signals for today.

    market_data: {symbol: pd.DataFrame [High, Low, Close]}
    Returns list of signal dicts sorted by RSI distance (most extreme first).
    """
    if open_symbols is None:
        open_symbols = set()

    es_risk_off = _es_risk_off(market_data, regime_symbol)
    signals     = []

    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        rsi_s   = _rsi(c)
        sma50   = _sma(c, TREND_SMA)
        atr_s   = _atr(h, l, c)

        cur_rsi   = float(rsi_s.iloc[-1])
        cur_sma   = float(sma50.iloc[-1])
        cur_close = float(c.iloc[-1])
        cur_atr   = float(atr_s.iloc[-1])

        if np.isnan(cur_rsi) or np.isnan(cur_sma) or cur_atr <= 0:
            continue

        in_uptrend   = cur_close > cur_sma
        in_downtrend = cur_close < cur_sma

        if sym in EQUITY_FUTURES and es_risk_off:
            in_uptrend = False   # regime filter blocks equity longs

        # Long signal: in uptrend + RSI oversold
        if in_uptrend and cur_rsi <= RSI_OVERSOLD:
            stop  = cur_close - ATR_STOP_MULT * cur_atr
            score = RSI_OVERSOLD - cur_rsi   # lower RSI = more extreme = higher priority
            signals.append({
                "symbol":     sym,
                "direction":  "Buy",
                "score":      float(score),
                "rsi":        cur_rsi,
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
            })

        # Short signal: in downtrend + RSI overbought (bidirectional only)
        elif sym in BIDIRECTIONAL_MARKETS and in_downtrend and cur_rsi >= RSI_OVERBOUGHT:
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


def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int) -> tuple:
    """RSI-based exit: return (exit_flag, reason)."""
    if df is None or len(df) < RSI_PERIOD + 2:
        return False, ""

    h, l, c  = df["High"], df["Low"], df["Close"]
    rsi_s    = _rsi(c)
    cur_rsi  = float(rsi_s.iloc[-1])
    cur_close = float(c.iloc[-1])
    cur_high  = float(h.iloc[-1])
    cur_low   = float(l.iloc[-1])

    direction  = position["direction"]
    stop_price = position["stop_price"]

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    if direction == "Buy":
        if cur_rsi >= RSI_EXIT_LONG:
            return True, f"rsi_recovery ({cur_rsi:.1f} >= {RSI_EXIT_LONG})"
        if cur_low <= stop_price:
            return True, f"hard_stop ({stop_price:.4f})"
    else:
        if cur_rsi <= RSI_EXIT_SHORT:
            return True, f"rsi_recovery ({cur_rsi:.1f} <= {RSI_EXIT_SHORT})"
        if cur_high >= stop_price:
            return True, f"hard_stop ({stop_price:.4f})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  contract_size: float = 1.0) -> int:
    risk_amount   = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr * contract_size
    if stop_distance <= 0:
        return 1
    return max(1, int(risk_amount / stop_distance))


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    band = ATR_STOP_MULT * current_atr
    if direction == "Buy":
        return max(current_stop, current_price - band)
    return min(current_stop, current_price + band)


def scan_summary(market_data: dict) -> list:
    """RSI + SMA snapshot for all markets."""
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        cur_rsi   = float(_rsi(c).iloc[-1])
        cur_sma50 = float(_sma(c, TREND_SMA).iloc[-1])
        cur_close = float(c.iloc[-1])
        trend     = "BULL" if cur_close > cur_sma50 else "BEAR"
        ob_flag   = "OB" if cur_rsi > RSI_OVERBOUGHT else ("OS" if cur_rsi < RSI_OVERSOLD else "  ")
        rows.append({
            "symbol":  sym,
            "close":   cur_close,
            "rsi5":    cur_rsi,
            "sma50":   cur_sma50,
            "trend":   trend,
            "ob_flag": ob_flag,
            "status":  "ok",
        })
    return rows
