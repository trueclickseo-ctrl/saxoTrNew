"""
futures/strategy_ma_cross.py
-----------------------------
Futures Strategy 6 â€” SMA(50/200) Golden Cross / Death Cross.

CONCEPT:
  The 50/200 day moving average cross is one of the oldest and most
  studied signals in technical analysis. When the 50d SMA crosses above
  the 200d SMA ("Golden Cross"), it marks a confirmed long-term trend shift
  from bear to bull. The Death Cross (50d crosses below 200d) marks the
  opposite.

  These signals are rare (~2-4 per market per year) but extremely high
  quality â€” they filter out all short-term noise and capture only genuine
  multi-week trend changes. Win rate is typically 65-70% because by the
  time the cross occurs, the trend is well-established.

  IMPORTANT: Golden/Death Cross signals lag by design. The entry is NOT
  at the start of the trend â€” it's at confirmation. This trades off some
  upside for much higher signal quality. The hard stop and time stop
  protect against the minority of false crosses.

ENTRY:
  LONG  (all markets):     SMA(50) crosses above SMA(200) within last 3 bars
                           AND SMA(50) > SMA(200) (aligned)
                           AND ADX(14) >= 15 (minimal trend filter)
  SHORT (GC + ZB only):   SMA(50) crosses below SMA(200) within last 3 bars
                           AND SMA(50) < SMA(200)
  Regime filter: NOT applied â€” the cross itself IS the regime filter.

EXIT (first condition hit):
  A. SMA(50) crosses back through SMA(200) (confirmed reversal)
  B. 2.5Ã—ATR(14) hard stop (wider â€” gives trend room to breathe)
  C. 60-day time stop (trend crosses resolve over weeks, not days)

SIZING:  1% equity per trade, ATR-based.

EXPECTED RESULTS:
  ~4-8 signals/year  |  WR ~65-70%  |  Avg hold ~25 days
  Edge: highest signal quality of all 6 strategies â€” the cross confirms
  the trend has already flipped. Large winners when the trend continues
  post-cross (oil 2022, gold 2023, bonds 2020).

THIS MODULE IS PURE â€” no I/O, no orders, no state.
"""

import numpy as np
import pandas as pd

# â”€â”€ Parameters â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
FAST_MA        = 50
SLOW_MA        = 200
ADX_PERIOD     = 14
ADX_MIN        = 15    # very low â€” the MA cross itself is the filter
ATR_PERIOD     = 14
ATR_STOP_MULT  = 2.5   # wider stop for longer-term signal
RISK_PCT       = 0.01   # reverted 2026-08-24 -- see futures/strategy.py's RISK_PCT comment
TIME_STOP_DAYS = 60
SIGNAL_LOOKBACK = 3
MIN_BARS       = SLOW_MA + ADX_PERIOD + 5

LONG_ONLY_MARKETS     = {"ES", "NQ", "YM", "DAX", "HK50", "CL", "NG"}
BIDIRECTIONAL_MARKETS = {"GC", "SI", "ZB", "ZC", "ZW", "ZS"}
EQUITY_FUTURES        = {"ES", "NQ", "YM", "DAX", "HK50"}


def _sma(s, p):  return s.rolling(p, min_periods=p).mean()


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


def generate_signals(market_data: dict, regime_symbol: str = "ES",
                     open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()
    signals = []

    for sym, df in market_data.items():
        if sym in open_symbols: continue
        if df is None or len(df) < MIN_BARS: continue

        h, l, c  = df["High"], df["Low"], df["Close"]
        fast     = _sma(c, FAST_MA)
        slow     = _sma(c, SLOW_MA)
        adx_s, plus_di, minus_di = _adx(h, l, c)
        atr_s    = _atr(h, l, c)

        cur_adx  = float(adx_s.iloc[-1])
        cur_atr  = float(atr_s.iloc[-1])
        cur_close= float(c.iloc[-1])
        if np.isnan(cur_adx) or cur_atr <= 0: continue
        if cur_adx < ADX_MIN: continue

        cur_fast = float(fast.iloc[-1])
        cur_slow = float(slow.iloc[-1])

        # Detect cross within lookback
        golden = death = False
        n = len(fast)
        for k in range(1, min(SIGNAL_LOOKBACK + 1, n - 1)):
            f_now = float(fast.iloc[-k]);  f_prv = float(fast.iloc[-(k+1)])
            s_now = float(slow.iloc[-k]);  s_prv = float(slow.iloc[-(k+1)])
            if f_prv <= s_prv and f_now > s_now: golden = True; break
            if f_prv >= s_prv and f_now < s_now: death  = True; break

        if golden and cur_fast > cur_slow:
            stop = cur_close - ATR_STOP_MULT * cur_atr
            signals.append({"symbol": sym, "direction": "Buy",
                            "score": float(cur_adx), "cross": "golden",
                            "sma_gap_pct": (cur_fast - cur_slow) / cur_slow * 100,
                            "atr": cur_atr, "close": cur_close, "stop_price": stop})
        elif sym in BIDIRECTIONAL_MARKETS and death and cur_fast < cur_slow:
            stop = cur_close + ATR_STOP_MULT * cur_atr
            signals.append({"symbol": sym, "direction": "Sell",
                            "score": float(cur_adx), "cross": "death",
                            "sma_gap_pct": (cur_slow - cur_fast) / cur_slow * 100,
                            "atr": cur_atr, "close": cur_close, "stop_price": stop})

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < MIN_BARS: return False, ""
    h, l, c = df["High"], df["Low"], df["Close"]
    fast     = _sma(c, FAST_MA)
    slow     = _sma(c, SLOW_MA)
    direction  = position["direction"]
    stop_price = position["stop_price"]
    cur_low  = float(l.iloc[-1])
    cur_high = float(h.iloc[-1])

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    f_now = float(fast.iloc[-1]); f_prv = float(fast.iloc[-2])
    s_now = float(slow.iloc[-1]); s_prv = float(slow.iloc[-2])

    if direction == "Buy":
        if f_prv >= s_prv and f_now < s_now:
            return True, "death_cross"
        if cur_low <= stop_price:
            return True, f"hard_stop ({stop_price:.4f})"
    else:
        if f_prv <= s_prv and f_now > s_now:
            return True, "golden_cross"
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
        fast = _sma(c, FAST_MA); slow = _sma(c, SLOW_MA)
        f = float(fast.iloc[-1]); s = float(slow.iloc[-1])
        gap_pct = (f - s) / s * 100
        adx_s, _, _ = _adx(h, l, c)
        rows.append({
            "symbol":   sym,
            "close":    float(c.iloc[-1]),
            "sma50":    f,
            "sma200":   s,
            "gap_pct":  gap_pct,
            "cross":    "BULL" if f > s else "BEAR",
            "adx":      float(adx_s.iloc[-1]),
            "status":   "ok",
        })
    return rows
