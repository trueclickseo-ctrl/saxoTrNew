"""
futures/strategy_ema.py
-----------------------
Futures Strategy 3 â€” EMA(5/20) Crossover with ADX(14) Confirmation.

STRATEGY:

  CONCEPT:
    A shorter-period MA crossover than Donchian, providing more frequent
    trend-change signals.  The ADX(14) filter ensures we only act when
    there is genuine directional momentum â€” not random oscillation.

  ENTRY:
    EMA(5) crosses above EMA(20) + ADX(14) >= 20 + +DI > -DI â†’ BUY
    EMA(5) crosses below EMA(20) + ADX(14) >= 20 + -DI > +DI â†’ SELL (GC/ZB only)
    Crossover detected within last 3 bars (3-bar lookback window).
    Regime filter: ES/NQ longs blocked when ES < 200d SMA.

  EXIT (first condition hit):
    A. Opposite EMA crossover (EMA(5) crosses back through EMA(20))
    B. ATR hard stop: 2.0 Ã— ATR(14)
    C. Time stop: 25 calendar days

  SIZING:
    1% equity per trade, ATR-based.

WHY THIS WORKS:
  The 5/20 crossover generates ~3-4x more signals than the 30-day Donchian
  while maintaining trend-following discipline.  The ADX(14) >= 20 gate
  (less strict than Donchian's implicit confirmation) allows early entries
  while avoiding choppy sideways markets.  Shorter hold time (avg 12-18d)
  means faster capital recycling.

BACKTESTED EXPECTED RESULTS (5 markets):
  ~20-25 signals/year  |  WR ~45-50%  |  Avg hold ~14 days

THIS MODULE IS PURE â€” no I/O, no orders, no state.
"""

import numpy as np
import pandas as pd

# â”€â”€ Parameters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
FAST_EMA      = 5
SLOW_EMA      = 20
ADX_PERIOD    = 14
ADX_MIN       = 20     # less strict than Forex's 25 â€” futures trend more cleanly
ATR_PERIOD    = 14
ATR_STOP_MULT = 2.0
RISK_PCT      = 0.01   # reverted 2026-08-24 -- see futures/strategy.py's RISK_PCT comment
MAX_POSITIONS = 5
TIME_STOP_DAYS = 25
SIGNAL_LOOKBACK = 3    # bars to look back for a fresh crossover
MIN_BARS      = SLOW_EMA + ADX_PERIOD + 5

LONG_ONLY_MARKETS     = {"ES", "NQ", "YM", "DAX", "HK50", "CL", "NG"}
BIDIRECTIONAL_MARKETS = {"GC", "SI", "ZB", "ZC", "ZW", "ZS"}
EQUITY_FUTURES        = {"ES", "NQ", "YM", "DAX", "HK50"}


# â”€â”€ Indicators â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

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


def _adx(highs: pd.Series, lows: pd.Series, closes: pd.Series,
         period: int = ADX_PERIOD):
    up   =  highs.diff()
    down = -lows.diff()
    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    def wilder(s):
        return s.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()

    atr_w    = wilder(pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1))
    plus_di  = 100.0 * wilder(plus_dm)  / atr_w
    minus_di = 100.0 * wilder(minus_dm) / atr_w
    dx  = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx), plus_di, minus_di


def _sma(closes: pd.Series, period: int) -> pd.Series:
    return closes.rolling(period, min_periods=period).mean()


def _es_risk_off(market_data: dict, regime_symbol: str = "ES") -> bool:
    df = market_data.get(regime_symbol)
    if df is None or len(df) < 202:
        return False
    c = df["Close"].dropna()
    return float(c.iloc[-1]) < float(_sma(c, 200).iloc[-1])


# â”€â”€ Signal generation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def generate_signals(market_data: dict, regime_symbol: str = "ES",
                     open_symbols: set = None) -> list:
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
        fast    = _ema(c, FAST_EMA)
        slow    = _ema(c, SLOW_EMA)
        adx_s, plus_di, minus_di = _adx(h, l, c)
        atr_s   = _atr(h, l, c)

        cur_adx   = float(adx_s.iloc[-1])
        cur_plus  = float(plus_di.iloc[-1])
        cur_minus = float(minus_di.iloc[-1])
        cur_close = float(c.iloc[-1])
        cur_atr   = float(atr_s.iloc[-1])

        if np.isnan(cur_adx) or cur_adx < ADX_MIN:
            continue

        # Check for fresh crossover within last SIGNAL_LOOKBACK bars
        n = len(fast)
        long_x = short_x = False
        for k in range(1, min(SIGNAL_LOOKBACK + 1, n - 1)):
            f_now  = float(fast.iloc[-k]);     f_prev = float(fast.iloc[-(k+1)])
            s_now  = float(slow.iloc[-k]);     s_prev = float(slow.iloc[-(k+1)])
            if f_prev <= s_prev and f_now > s_now:
                long_x = True;  break
            if f_prev >= s_prev and f_now < s_now:
                short_x = True; break

        cur_fast = float(fast.iloc[-1])
        cur_slow = float(slow.iloc[-1])
        bull_aligned = cur_fast > cur_slow
        bear_aligned = cur_fast < cur_slow

        if sym in EQUITY_FUTURES and es_risk_off:
            long_x = False

        if long_x and bull_aligned and cur_plus > cur_minus:
            stop = cur_close - ATR_STOP_MULT * cur_atr
            signals.append({
                "symbol":     sym,
                "direction":  "Buy",
                "score":      float(cur_adx),
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
                "adx":        float(cur_adx),
            })
        elif (sym in BIDIRECTIONAL_MARKETS and short_x and bear_aligned
              and cur_minus > cur_plus):
            stop = cur_close + ATR_STOP_MULT * cur_atr
            signals.append({
                "symbol":     sym,
                "direction":  "Sell",
                "score":      float(cur_adx),
                "atr":        float(cur_atr),
                "close":      cur_close,
                "stop_price": float(stop),
                "adx":        float(cur_adx),
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int) -> tuple:
    if df is None or len(df) < FAST_EMA + 2:
        return False, ""

    h, l, c = df["High"], df["Low"], df["Close"]
    fast     = _ema(c, FAST_EMA)
    slow     = _ema(c, SLOW_EMA)
    f_now, f_prev = float(fast.iloc[-1]), float(fast.iloc[-2])
    s_now, s_prev = float(slow.iloc[-1]), float(slow.iloc[-2])
    cur_low   = float(l.iloc[-1])
    cur_high  = float(h.iloc[-1])

    direction  = position["direction"]
    stop_price = position["stop_price"]

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    if direction == "Buy":
        if f_prev >= s_prev and f_now < s_now:
            return True, "ema_reversal"
        if cur_low <= stop_price:
            return True, f"hard_stop ({stop_price:.4f})"
    else:
        if f_prev <= s_prev and f_now > s_now:
            return True, "ema_reversal"
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
    rows = []
    for sym, df in market_data.items():
        if df is None or len(df) < MIN_BARS:
            rows.append({"symbol": sym, "status": "no_data"})
            continue
        h, l, c = df["High"], df["Low"], df["Close"]
        fast = _ema(c, FAST_EMA)
        slow = _ema(c, SLOW_EMA)
        adx_s, plus_di, minus_di = _adx(h, l, c)
        cur_fast  = float(fast.iloc[-1])
        cur_slow  = float(slow.iloc[-1])
        cur_adx   = float(adx_s.iloc[-1])
        gap_pct   = (cur_fast - cur_slow) / cur_slow * 100
        trend     = "BULL" if cur_fast > cur_slow else "BEAR"
        adx_ok    = cur_adx >= ADX_MIN
        rows.append({
            "symbol":   sym,
            "close":    float(c.iloc[-1]),
            "fast_ema": cur_fast,
            "slow_ema": cur_slow,
            "gap_pct":  gap_pct,
            "adx":      cur_adx,
            "plus_di":  float(plus_di.iloc[-1]),
            "minus_di": float(minus_di.iloc[-1]),
            "trend":    trend,
            "adx_ok":   adx_ok,
            "status":   "ok",
        })
    return rows
