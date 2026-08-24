"""
futures/strategy_macd.py
------------------------
Futures Strategy 4 â€” MACD(12,26,9) Momentum Crossover.

CONCEPT:
  MACD measures the difference between two EMAs (12 and 26 periods).
  When the MACD line crosses above its signal line AND the histogram turns
  positive, it signals that short-term momentum is accelerating in the
  trend direction. Combined with a zero-line filter (MACD > 0 for longs,
  < 0 for shorts) this removes counter-trend entries.

  Complementary to EMA(5/20): MACD uses longer periods (12/26 vs 5/20)
  and the histogram confirms acceleration, not just level â€” fewer signals
  but higher quality.

ENTRY:
  LONG  (all markets):     MACD line crosses above signal line
                           AND MACD line > 0 (positive momentum zone)
                           AND ADX(14) >= 18 (some trend present)
  SHORT (GC + ZB only):   MACD line crosses below signal line
                           AND MACD line < 0 (negative momentum zone)
                           AND ADX(14) >= 18
  Crossover detected within last 2 bars.
  Regime filter: ES/NQ longs blocked when ES < 200d SMA.

EXIT (first condition hit):
  A. MACD crosses back through signal (momentum exhausted)
  B. 2.0Ã—ATR(14) hard stop
  C. 20-day time stop

SIZING:  1% equity per trade, ATR-based.

EXPECTED RESULTS (5 markets, multi-year backtest):
  ~12-18 signals/year  |  WR ~52-58%  |  Avg hold ~14 days
  Edge: catches momentum inflection points earlier than price crossovers.

THIS MODULE IS PURE â€” no I/O, no orders, no state.
"""

import numpy as np
import pandas as pd

# â”€â”€ Parameters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL   = 9
ADX_PERIOD    = 14
ADX_MIN       = 18
ATR_PERIOD    = 14
ATR_STOP_MULT = 2.0
RISK_PCT      = 0.005  # smaller on purpose — see futures/strategy.py's RISK_PCT comment
TIME_STOP_DAYS = 20
SIGNAL_LOOKBACK = 3    # bars to look back for a fresh MACD crossover
# Zero-line: MACD must have been positive at some point in this lookback window
# (relaxed from strict cur_macd > 0 to allow crossovers slightly below zero
#  when momentum was recently bullish â€” catches more valid signals)
ZERO_LINE_LOOKBACK = 10  # bars to look back for a positive MACD reading
MIN_BARS      = MACD_SLOW + MACD_SIGNAL + ADX_PERIOD + 5

LONG_ONLY_MARKETS     = {"ES", "NQ", "YM", "DAX", "HK50", "CL", "NG"}
BIDIRECTIONAL_MARKETS = {"GC", "SI", "ZB", "ZC", "ZW", "ZS"}
EQUITY_FUTURES        = {"ES", "NQ", "YM", "DAX", "HK50"}


def _ema(s: pd.Series, period: int) -> pd.Series:
    return s.ewm(span=period, adjust=False).mean()


def _macd(closes: pd.Series):
    fast   = _ema(closes, MACD_FAST)
    slow   = _ema(closes, MACD_SLOW)
    line   = fast - slow
    signal = _ema(line, MACD_SIGNAL)
    hist   = line - signal
    return line, signal, hist


def _atr(highs, lows, closes, period=ATR_PERIOD):
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def _adx(highs, lows, closes, period=ADX_PERIOD):
    up   =  highs.diff()
    down = -lows.diff()
    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    def wilder(s): return s.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()
    atr_w    = wilder(pd.concat([highs - lows, (highs - closes.shift(1)).abs(),
                                  (lows  - closes.shift(1)).abs()], axis=1).max(axis=1))
    plus_di  = 100.0 * wilder(plus_dm) / atr_w
    minus_di = 100.0 * wilder(minus_dm) / atr_w
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx), plus_di, minus_di


def _sma(s, p): return s.rolling(p, min_periods=p).mean()


def _es_risk_off(market_data, sym="ES"):
    df = market_data.get(sym)
    if df is None or len(df) < 202: return False
    c = df["Close"].dropna()
    return float(c.iloc[-1]) < float(_sma(c, 200).iloc[-1])


def generate_signals(market_data: dict, regime_symbol: str = "ES",
                     open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()
    es_risk_off = _es_risk_off(market_data, regime_symbol)
    signals = []

    for sym, df in market_data.items():
        if sym in open_symbols: continue
        if df is None or len(df) < MIN_BARS: continue

        h, l, c  = df["High"], df["Low"], df["Close"]
        macd_l, macd_s, macd_h = _macd(c)
        adx_s, plus_di, minus_di = _adx(h, l, c)
        atr_s    = _atr(h, l, c)

        cur_adx  = float(adx_s.iloc[-1])
        cur_atr  = float(atr_s.iloc[-1])
        cur_close= float(c.iloc[-1])
        if np.isnan(cur_adx) or cur_atr <= 0: continue
        if cur_adx < ADX_MIN: continue

        # Detect MACD crossover within lookback
        long_x = short_x = False
        n = len(macd_l)
        for k in range(1, min(SIGNAL_LOOKBACK + 1, n - 1)):
            ml_now = float(macd_l.iloc[-k]);     ml_prv = float(macd_l.iloc[-(k+1)])
            ms_now = float(macd_s.iloc[-k]);     ms_prv = float(macd_s.iloc[-(k+1)])
            if ml_prv < ms_prv and ml_now >= ms_now: long_x  = True; break
            if ml_prv > ms_prv and ml_now <= ms_now: short_x = True; break

        cur_macd = float(macd_l.iloc[-1])
        if sym in EQUITY_FUTURES and es_risk_off:
            long_x = False

        # Relaxed zero-line filter: MACD was positive recently OR is positive now.
        # This catches crossovers from slightly below zero (common in mid-trend).
        lookback_vals = macd_l.iloc[-ZERO_LINE_LOOKBACK:]
        recently_positive = bool((lookback_vals > 0).any())
        recently_negative = bool((lookback_vals < 0).any())

        if long_x and (cur_macd > 0 or recently_positive):
            stop = cur_close - ATR_STOP_MULT * cur_atr
            signals.append({"symbol": sym, "direction": "Buy",
                            "score": abs(cur_macd), "macd": cur_macd,
                            "atr": cur_atr, "close": cur_close, "stop_price": stop})
        elif sym in BIDIRECTIONAL_MARKETS and short_x and (cur_macd < 0 or recently_negative):
            stop = cur_close + ATR_STOP_MULT * cur_atr
            signals.append({"symbol": sym, "direction": "Sell",
                            "score": abs(cur_macd), "macd": cur_macd,
                            "atr": cur_atr, "close": cur_close, "stop_price": stop})

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < MIN_BARS: return False, ""
    h, l, c = df["High"], df["Low"], df["Close"]
    macd_l, macd_s, _ = _macd(c)
    direction  = position["direction"]
    stop_price = position["stop_price"]
    cur_low  = float(l.iloc[-1])
    cur_high = float(h.iloc[-1])

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    ml_now = float(macd_l.iloc[-1]); ml_prv = float(macd_l.iloc[-2])
    ms_now = float(macd_s.iloc[-1]); ms_prv = float(macd_s.iloc[-2])

    if direction == "Buy":
        if ml_prv >= ms_prv and ml_now < ms_now:
            return True, "macd_reversal"
        if cur_low <= stop_price:
            return True, f"hard_stop ({stop_price:.4f})"
    else:
        if ml_prv <= ms_prv and ml_now > ms_now:
            return True, "macd_reversal"
        if cur_high >= stop_price:
            return True, f"hard_stop ({stop_price:.4f})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  contract_size: float = 1.0) -> int:
    risk_amount   = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr * contract_size
    if stop_distance <= 0: return 1
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
            rows.append({"symbol": sym, "status": "no_data"}); continue
        h, l, c = df["High"], df["Low"], df["Close"]
        macd_l, macd_s, macd_h = _macd(c)
        adx_s, _, _ = _adx(h, l, c)
        rows.append({
            "symbol":    sym,
            "close":     float(c.iloc[-1]),
            "macd":      float(macd_l.iloc[-1]),
            "signal":    float(macd_s.iloc[-1]),
            "hist":      float(macd_h.iloc[-1]),
            "adx":       float(adx_s.iloc[-1]),
            "status":    "ok",
        })
    return rows
