"""
forex/strategy_donchian_quality.py
------------------------------------
FX Strategy — "Donchian Quality" (2026-08-29).

Explicit user request: implement a detailed design doc's breakout-quality
filters as a NEW parallel strategy, run it alongside the existing
"donchian" strategy unchanged, and compare which performs better --
"do not change your actual donchian strategy let it run normal."
`forex/strategy_donchian.py` is completely untouched; this is a
from-scratch module registered as its own STRATEGIES key
("donchian_quality") in forex/runner.py.

WHAT'S DIFFERENT vs strategy_donchian.py, and why (per the user's design doc):

1. Minimum breakout-strength filter (MUST) -- the original computed
   breakout_strength = (today - high30) / atr only for RANKING, never for
   filtering, so a close a fraction of a pip above the 30-day high fired
   the same as a real breakout. Now requires
   MIN_BREAKOUT_ATR <= breakout_strength <= MAX_BREAKOUT_ATR (0.10-1.50) --
   too small is noise, too large risks chasing an already-exhausted move.

2. ADX must be RISING, not just above threshold -- ADX can stay above 25
   while the trend that produced it is already decelerating. Now also
   requires adx_val > adx_series.iloc[-(1+ADX_RISING_LOOKBACK)] (2 bars
   back by default).

3. Maximum distance from EMA(200), in ATR units -- blocks a breakout that
   only clears the trend filter numerically while price is already
   MAX_EMA_DISTANCE_ATR (3.0) or more ATRs away from the EMA, a signature
   of a late/extended move rather than a fresh one.

4. Ranking is no longer implicitly "biggest breakout wins" -- capping at
   MAX_BREAKOUT_ATR (item 1) already removes the most extreme outliers
   from consideration entirely, so what's left ranks within a bounded,
   more comparable 0.10-1.50 ATR band rather than an unbounded one.

5. MAX_POSITIONS is a REAL enforced cap here, not vestigial. Verified live
   2026-08-29: the original "donchian" strategy's own MAX_POSITIONS = 4
   constant is never actually read by forex/runner.py -- the runner's real
   concurrent-position cap comes from SLOTS_PER_STRATEGY[strat_name], which
   for "donchian" is set to _SWING_SLOTS (the FULL pair universe, 149+ --
   nowhere near 4). This module's own MAX_POSITIONS is wired into
   forex/runner.py's SLOTS_PER_STRATEGY as a REAL cap for "donchian_quality"
   specifically (the original "donchian" is left exactly as-is, per "let it
   run normal").

6. Trailing stop -- confirmed live 2026-08-29 that forex/runner.py already
   calls trailing_stop_update() generically for ANY strategy module that
   defines one (via hasattr(strat_mod, "trailing_stop_update")) -- this
   was NOT a gap in the original, and needs no fix. This module defines
   the identical function so the same generic mechanism applies to it too.

7. scan_summary()'s "high20"/"low20" naming (which actually computed a
   30-day channel, a pure diagnostic-only naming bug in the original) is
   correctly named high30/low30 here.

THIS MODULE IS PURE — no I/O, no orders, no state. All execution lives in
forex/runner.py, same interface contract as every other strategy module.
"""

import numpy as np
import pandas as pd

BREAKOUT_PERIOD      = 30
EXIT_PERIOD          = 15
ATR_PERIOD           = 14
EMA_TREND            = 200
ADX_PERIOD           = 14
ADX_MIN              = 25
ADX_RISING_LOOKBACK  = 2      # bars back to compare ADX against for the "rising" check
ATR_STOP_MULT        = 2.0
RISK_PCT             = 0.0025
MAX_POSITIONS        = 4      # actually enforced -- see SLOTS_PER_STRATEGY wiring in runner.py
TIME_STOP_DAYS       = 30
LOT_ROUND            = 1_000
MIN_BARS             = EMA_TREND + ATR_PERIOD + 5

# ── New quality filters (item 1, 2, 3) ──────────────────────────────────
MIN_BREAKOUT_ATR     = 0.10   # tiny channel break -> skip (noise)
MAX_BREAKOUT_ATR     = 1.50   # huge breakout -> skip (potentially exhausted/news-driven)
MAX_EMA_DISTANCE_ATR = 3.0    # too far from EMA200 -> skip (late/extended trend)


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
    """Donchian breakout with quality filters: minimum AND maximum breakout
    strength (ATR units), ADX must be rising (not just above threshold),
    and a maximum allowed distance from EMA(200) (ATR units)."""
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS + ADX_RISING_LOOKBACK:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        today       = float(c.iloc[-1])
        history     = c.iloc[-(BREAKOUT_PERIOD + 1):-1]
        high30      = float(history.max())
        low30       = float(history.min())
        atr_val     = float(_atr(h, l, c).iloc[-1])
        ema200      = float(c.ewm(span=EMA_TREND, adjust=False).mean().iloc[-1])
        adx_series  = _adx(h, l, c)
        adx_val     = float(adx_series.iloc[-1])
        adx_prev    = float(adx_series.iloc[-(1 + ADX_RISING_LOOKBACK)])

        if np.isnan(high30) or np.isnan(low30) or atr_val <= 0:
            continue
        if np.isnan(ema200) or np.isnan(adx_val) or np.isnan(adx_prev):
            continue
        if adx_val < ADX_MIN:                 # item: trend not strong enough
            continue
        if adx_val <= adx_prev:                # item 2: ADX must be RISING, not just high
            continue

        if today > high30 and today > ema200:                # breakout WITH macro trend
            breakout_strength = (today - high30) / atr_val
            ema_distance      = (today - ema200) / atr_val
            if breakout_strength < MIN_BREAKOUT_ATR or breakout_strength > MAX_BREAKOUT_ATR:
                continue   # item 1: too small (noise) or too large (exhausted/news) -- skip
            if ema_distance > MAX_EMA_DISTANCE_ATR:
                continue   # item 3: too far extended from EMA200 -- skip
            stop  = today - ATR_STOP_MULT * atr_val
            signals.append({
                "symbol":             sym,
                "direction":          "Buy",
                "score":              float(breakout_strength),
                "atr":                float(atr_val),
                "close":              today,
                "stop_price":         float(stop),
                "breakout_level":     float(high30),
                "adx":                float(adx_val),
                "breakout_strength":  float(breakout_strength),
                "ema_distance_atr":   float(ema_distance),
            })
        elif today < low30 and today < ema200:               # breakdown WITH macro trend
            breakout_strength = (low30 - today) / atr_val
            ema_distance      = (ema200 - today) / atr_val
            if breakout_strength < MIN_BREAKOUT_ATR or breakout_strength > MAX_BREAKOUT_ATR:
                continue
            if ema_distance > MAX_EMA_DISTANCE_ATR:
                continue
            stop  = today + ATR_STOP_MULT * atr_val
            signals.append({
                "symbol":             sym,
                "direction":          "Sell",
                "score":              float(breakout_strength),
                "atr":                float(atr_val),
                "close":              today,
                "stop_price":         float(stop),
                "breakout_level":     float(low30),
                "adx":                float(adx_val),
                "breakout_strength":  float(breakout_strength),
                "ema_distance_atr":   float(ema_distance),
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Unchanged from strategy_donchian.py -- the user's design doc didn't
    ask for exit-logic changes, only entry-quality filters."""
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
        low15 = float(c.iloc[-(EXIT_PERIOD + 1):-1].min())
        if today <= low15:
            return True, f"donchian_exit ({EXIT_PERIOD}d low {low15:.5f})"
    else:
        if stop_px > 0 and float(h.iloc[-1]) >= stop_px:
            return True, f"hard_stop ({stop_px:.5f})"
        high15 = float(c.iloc[-(EXIT_PERIOD + 1):-1].max())
        if today >= high15:
            return True, f"donchian_exit ({EXIT_PERIOD}d high {high15:.5f})"

    return False, ""


def size_position(account_equity: float, atr: float,
                  min_units: float = 1_000, block_below_min: bool = False) -> int:
    """Unchanged sizing math from strategy_donchian.py."""
    risk_amount   = account_equity * RISK_PCT
    stop_distance = ATR_STOP_MULT * atr
    if stop_distance <= 0:
        return 0 if block_below_min else int(min_units)
    raw     = risk_amount / stop_distance
    floored = int(raw / min_units) * int(min_units)
    if floored < min_units:
        return 0 if block_below_min else int(min_units)
    return floored


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    """Identical to strategy_donchian.py's -- forex/runner.py already calls
    this generically for any strategy module that defines it (confirmed
    live 2026-08-29, see this module's docstring item 6)."""
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
        # Item 7 fix: these are a BREAKOUT_PERIOD(30)-bar channel -- named
        # correctly here (the original strategy_donchian.py's scan_summary
        # calls the identical 30-bar values "high20"/"low20", a pure
        # diagnostic-naming bug that doesn't affect trading signals but
        # confuses anyone reading the CLI --scan output).
        high30   = float(history.max())
        low30    = float(history.min())
        atr_val  = float(_atr(h, l, c).iloc[-1])
        gap_hi   = (today - high30) / atr_val if atr_val > 0 else 0
        gap_lo   = (low30 - today)  / atr_val if atr_val > 0 else 0
        if today > high30:
            signal = "BREAKOUT!"
        elif today < low30:
            signal = "BREAKDOWN!"
        else:
            pct_pos = (today - low30) / (high30 - low30) * 100 if high30 != low30 else 50
            signal  = f"range {pct_pos:.0f}%"
        rows.append({
            "symbol":  sym, "close": today,
            "high30":  high30, "low30": low30,
            "atr":     atr_val, "gap_hi": gap_hi, "gap_lo": gap_lo,
            "signal":  signal,  "status": "ok",
        })
    return rows
