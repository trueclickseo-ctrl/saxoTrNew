"""
futures/strategy_squeeze.py
---------------------------
Futures Strategy 5 — Bollinger Band Squeeze Breakout.

CONCEPT:
  Markets alternate between periods of low volatility (compression) and
  high volatility (expansion). A "squeeze" occurs when Bollinger Bands
  (BB, 20d, 2σ) contract inside Keltner Channels (KC, 20d EMA ± 1.5×ATR).
  When BB eventually expands back outside KC, it signals an imminent
  directional breakout — volatility is releasing.

  Inspired by John Carter's TTM Squeeze (popularised in "Mastering the Trade").
  The squeeze itself is neutral — direction is determined by a momentum
  oscillator (close − midpoint of 20d high/low range) and ATR-confirmed breakout.

ENTRY:
  LONG:  squeeze just released (BB was inside KC, now BB > KC)
         AND momentum > 0 (close above midpoint of recent range)
         AND close > EMA(20) (price above dynamic support)
  SHORT: squeeze just released
         AND momentum < 0
         AND close < EMA(20)
         Only GC and ZB (bidirectional markets).
  Regime filter: ES/NQ longs blocked when ES < 200d SMA.

EXIT (first condition hit):
  A. Momentum reverses sign (histogram turns from + to − or vice versa)
  B. 2.0×ATR hard stop
  C. 15-day time stop (squeezes resolve fast — stale signals cut early)

SIZING:  1% equity per trade, ATR-based.

EXPECTED RESULTS:
  ~10-15 signals/year  |  WR ~60-65%  |  Avg hold ~8 days
  Edge: enters at the very start of a volatility expansion → tight stop,
  large potential move relative to risk.

THIS MODULE IS PURE — no I/O, no orders, no state.
"""

import numpy as np
import pandas as pd

# ── Parameters ────────────────────────────────────────────────────────────────
BB_PERIOD      = 20
BB_STD         = 2.0
KC_EMA_PERIOD  = 20
KC_ATR_MULT    = 1.5
ATR_PERIOD     = 14
ATR_STOP_MULT  = 2.0
RISK_PCT       = 0.01
TIME_STOP_DAYS = 15
MIN_BARS       = BB_PERIOD + ATR_PERIOD + 5

LONG_ONLY_MARKETS     = {"ES", "NQ", "CL"}
BIDIRECTIONAL_MARKETS = {"GC", "ZB"}
EQUITY_FUTURES        = {"ES", "NQ"}


def _ema(s, p):   return s.ewm(span=p, adjust=False).mean()
def _sma(s, p):   return s.rolling(p, min_periods=p).mean()


def _bb(closes, period=BB_PERIOD, n_std=BB_STD):
    mid   = _sma(closes, period)
    std   = closes.rolling(period, min_periods=period).std()
    upper = mid + n_std * std
    lower = mid - n_std * std
    width = upper - lower
    return upper, mid, lower, width


def _kc(highs, lows, closes, period=KC_EMA_PERIOD, mult=KC_ATR_MULT):
    mid = _ema(closes, period)
    tr  = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr   = tr.ewm(alpha=1.0/ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()
    upper = mid + mult * atr
    lower = mid - mult * atr
    return upper, mid, lower


def _momentum(highs, lows, closes, period=BB_PERIOD):
    """Close minus midpoint of the 20d high/low range (TTM momentum bar)."""
    high20 = highs.rolling(period, min_periods=period).max()
    low20  = lows.rolling(period, min_periods=period).min()
    mid    = (high20 + low20) / 2 + _sma(closes, period)
    return closes - mid / 2


def _atr(highs, lows, closes, period=ATR_PERIOD):
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows  - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, adjust=False, min_periods=period).mean()


def _es_risk_off(market_data, sym="ES"):
    df = market_data.get(sym)
    if df is None or len(df) < 202: return False
    c = df["Close"].dropna()
    return float(c.iloc[-1]) < float(_sma(c, 200).iloc[-1])


def _squeeze_on(bb_up, bb_lo, kc_up, kc_lo, i=-1):
    """True if BB is inside KC (compression)."""
    return float(bb_up.iloc[i]) < float(kc_up.iloc[i]) and \
           float(bb_lo.iloc[i]) > float(kc_lo.iloc[i])


def generate_signals(market_data: dict, regime_symbol: str = "ES",
                     open_symbols: set = None) -> list:
    if open_symbols is None:
        open_symbols = set()
    es_risk_off = _es_risk_off(market_data, regime_symbol)
    signals = []

    for sym, df in market_data.items():
        if sym in open_symbols: continue
        if df is None or len(df) < MIN_BARS: continue

        h, l, c = df["High"], df["Low"], df["Close"]
        bb_up, bb_mid, bb_lo, bb_wid = _bb(c)
        kc_up, kc_mid, kc_lo         = _kc(h, l, c)
        mom  = _momentum(h, l, c)
        ema20 = _ema(c, KC_EMA_PERIOD)
        atr_s = _atr(h, l, c)

        # Squeeze just released: was squeezed 1 bar ago, not squeezed now
        was_squeezed = _squeeze_on(bb_up, bb_lo, kc_up, kc_lo, i=-2)
        is_squeezed  = _squeeze_on(bb_up, bb_lo, kc_up, kc_lo, i=-1)
        if was_squeezed and not is_squeezed:
            # Squeeze just fired
            cur_mom   = float(mom.iloc[-1])
            cur_close = float(c.iloc[-1])
            cur_ema   = float(ema20.iloc[-1])
            cur_atr   = float(atr_s.iloc[-1])
            if np.isnan(cur_mom) or cur_atr <= 0: continue

            bullish  = cur_mom > 0 and cur_close > cur_ema
            bearish  = cur_mom < 0 and cur_close < cur_ema

            if sym in EQUITY_FUTURES and es_risk_off:
                bullish = False

            if bullish:
                stop = cur_close - ATR_STOP_MULT * cur_atr
                signals.append({"symbol": sym, "direction": "Buy",
                                "score": abs(cur_mom), "momentum": cur_mom,
                                "atr": cur_atr, "close": cur_close, "stop_price": stop})
            elif sym in BIDIRECTIONAL_MARKETS and bearish:
                stop = cur_close + ATR_STOP_MULT * cur_atr
                signals.append({"symbol": sym, "direction": "Sell",
                                "score": abs(cur_mom), "momentum": cur_mom,
                                "atr": cur_atr, "close": cur_close, "stop_price": stop})

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    if df is None or len(df) < MIN_BARS: return False, ""
    h, l, c = df["High"], df["Low"], df["Close"]
    mom      = _momentum(h, l, c)
    direction  = position["direction"]
    stop_price = position["stop_price"]
    cur_low  = float(l.iloc[-1])
    cur_high = float(h.iloc[-1])

    if calendar_days_held >= TIME_STOP_DAYS:
        return True, f"time_stop ({calendar_days_held}d)"

    cur_mom = float(mom.iloc[-1])
    prv_mom = float(mom.iloc[-2])

    if direction == "Buy":
        if prv_mom > 0 and cur_mom <= 0:
            return True, "momentum_reversal"
        if cur_low <= stop_price:
            return True, f"hard_stop ({stop_price:.4f})"
    else:
        if prv_mom < 0 and cur_mom >= 0:
            return True, "momentum_reversal"
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
        bb_up, bb_mid, bb_lo, bb_wid = _bb(c)
        kc_up, kc_mid, kc_lo         = _kc(h, l, c)
        mom  = _momentum(h, l, c)
        sq   = _squeeze_on(bb_up, bb_lo, kc_up, kc_lo)
        rows.append({
            "symbol":   sym,
            "close":    float(c.iloc[-1]),
            "bb_width": float(bb_wid.iloc[-1]),
            "momentum": float(mom.iloc[-1]),
            "squeeze":  sq,
            "status":   "ok",
        })
    return rows
