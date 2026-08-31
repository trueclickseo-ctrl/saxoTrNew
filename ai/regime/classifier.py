"""
ai/regime/classifier.py -- AI Sprint 1: market-regime classification.

A DETERMINISTIC function. No model, no API call. Price bars in, one of a
fixed set of regime labels out -- testable against known history exactly
the way strategy_learner.py is.

It is an INPUT FEATURE for the later AI signal-score agent (roadmap #1 ->
#2), not its own agent. Nothing calls this from forex/runner.py yet
(Sprint 1 exit criterion: a standalone, fully-tested utility).

Reuses forex.strategy's Wilder ADX/ATR/EMA (Sprint 1 plan: "reuse those,
don't reimplement"). Depends on forex, never the reverse.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from forex.strategy import _adx, _atr, _ema

# ── Taxonomy (roadmap doc; NEWS_DRIVEN is deferred -- needs the news layer) ──
LABELS = (
    "TRENDING_BULLISH", "TRENDING_BEARISH", "RANGING", "BREAKOUT",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "CHAOTIC",
)

# ── Thresholds. Fixed for trend strength (ADX is already scale-free), but
# volatility is judged RELATIVE to the pair's own recent median ATR so one
# set of numbers works for EURUSD and XAUUSD alike. Calibrated against a
# historical spot-check (ai_regime_spot_check.py) -- change with evidence,
# not by feel. ──
ADX_TREND        = 25.0     # >= this = a real trend (standard Wilder level)
ADX_WEAK         = 15.0     # < this = no directional structure
ADX_RISING_JUMP  = 8.0      # ADX gain over the last `_SLOPE_LOOKBACK` bars => "breaking out"
ATR_RATIO_HIGH   = 1.6      # current ATR vs its own recent median
ATR_RATIO_LOW    = 0.6
_SLOPE_LOOKBACK  = 10       # bars for the fast-MA slope and the ADX-rising check
_MA_FAST         = 20
_MA_SLOW         = 50
_MED_WINDOW      = 60       # bars for the "recent median ATR" baseline
MIN_BARS         = _MA_SLOW + _SLOPE_LOOKBACK + 5   # 65


def _to_frame(bars) -> pd.DataFrame | None:
    """Accept a list[dict] (High/Low/Close, Open optional) OR a DataFrame."""
    if isinstance(bars, pd.DataFrame):
        df = bars
    else:
        try:
            df = pd.DataFrame(list(bars))
        except Exception:
            return None
    if df is None or len(df) < MIN_BARS:
        return None
    for col in ("High", "Low", "Close"):
        if col not in df.columns:
            return None
    return df.reset_index(drop=True)


def classify_regime(bars) -> dict:
    """Classify the current regime from recent bars.

    Returns
    -------
    {
      "label":    one of LABELS, or "UNKNOWN" if there isn't enough data,
      "adx":      float, "plus_di": float, "minus_di": float,
      "atr_pct":  ATR as % of price,
      "atr_ratio": current ATR / its own recent-median ATR,
      "ma_slope": fast-EMA % change over the slope lookback,
      "confidence": 0..1, how strongly the winning rule held,
    }
    """
    df = _to_frame(bars)
    if df is None:
        return {"label": "UNKNOWN", "adx": None, "plus_di": None, "minus_di": None,
                "atr_pct": None, "atr_ratio": None, "ma_slope": None, "confidence": 0.0}

    h, l, c = df["High"].astype(float), df["Low"].astype(float), df["Close"].astype(float)

    adx_s, plus_s, minus_s = _adx(h, l, c)
    atr_s = _atr(h, l, c)
    ma_fast = _ema(c, _MA_FAST)

    adx      = float(adx_s.iloc[-1])
    plus_di  = float(plus_s.iloc[-1])
    minus_di = float(minus_s.iloc[-1])
    atr_now  = float(atr_s.iloc[-1])
    close    = float(c.iloc[-1])

    if any(np.isnan(x) for x in (adx, plus_di, minus_di, atr_now)) or close <= 0:
        return {"label": "UNKNOWN", "adx": None, "plus_di": None, "minus_di": None,
                "atr_pct": None, "atr_ratio": None, "ma_slope": None, "confidence": 0.0}

    atr_pct   = atr_now / close * 100.0
    atr_med   = float(atr_s.iloc[-_MED_WINDOW:].median())
    atr_ratio = (atr_now / atr_med) if atr_med > 0 else 1.0

    fast_then = float(ma_fast.iloc[-_SLOPE_LOOKBACK - 1])
    ma_slope  = ((float(ma_fast.iloc[-1]) - fast_then) / fast_then * 100.0) if fast_then else 0.0

    adx_then   = float(adx_s.iloc[-_SLOPE_LOOKBACK - 1])
    adx_rising = (not np.isnan(adx_then)) and (adx - adx_then) >= ADX_RISING_JUMP

    prior_hi = float(h.iloc[-_SLOPE_LOOKBACK - 1: -1].max())
    prior_lo = float(l.iloc[-_SLOPE_LOOKBACK - 1: -1].min())
    broke_out = close > prior_hi or close < prior_lo

    # ── decision order ────────────────────────────────────────────────────
    # 1. CHAOTIC  -- vol expansion with NO directional structure at all
    # 2. BREAKOUT -- ADX surging out of a quiet stretch AND the recent range
    #               is broken AND one DI clearly dominates: a vol expansion
    #               WITH direction, caught while ADX is still establishing
    #               (a fully-matured breakout reads as TRENDING further down)
    # 3/4. HIGH / LOW volatility -- vol extremes without (2)
    # 5/6. TRENDING_*  -- established ADX>=25 trend with matching slope
    # 7. RANGING       -- the default
    label: str
    conf: float
    _di_directional = abs(plus_di - minus_di) >= 12.0

    if atr_ratio >= ATR_RATIO_HIGH and adx < ADX_WEAK:
        label, conf = "CHAOTIC", min(1.0, (atr_ratio - ATR_RATIO_HIGH) + (ADX_WEAK - adx) / ADX_WEAK)
    elif adx_rising and broke_out and _di_directional and adx_then < ADX_TREND:
        label, conf = "BREAKOUT", min(1.0, (adx - adx_then) / 20.0)
    elif atr_ratio >= ATR_RATIO_HIGH:
        label, conf = "HIGH_VOLATILITY", min(1.0, (atr_ratio - ATR_RATIO_HIGH) / 0.8)
    elif atr_ratio <= ATR_RATIO_LOW:
        label, conf = "LOW_VOLATILITY", min(1.0, (ATR_RATIO_LOW - atr_ratio) / 0.4)
    elif adx >= ADX_TREND and plus_di > minus_di and ma_slope > 0:
        label, conf = "TRENDING_BULLISH", min(1.0, (adx - ADX_TREND) / 25.0 * 0.5
                                              + (plus_di - minus_di) / 40.0 * 0.5)
    elif adx >= ADX_TREND and minus_di > plus_di and ma_slope < 0:
        label, conf = "TRENDING_BEARISH", min(1.0, (adx - ADX_TREND) / 25.0 * 0.5
                                              + (minus_di - plus_di) / 40.0 * 0.5)
    else:
        label, conf = "RANGING", min(1.0, (ADX_TREND - adx) / ADX_TREND)

    return {
        "label": label,
        "adx": round(adx, 1), "plus_di": round(plus_di, 1), "minus_di": round(minus_di, 1),
        "atr_pct": round(atr_pct, 3), "atr_ratio": round(atr_ratio, 2),
        "ma_slope": round(ma_slope, 3), "confidence": round(max(0.0, min(1.0, conf)), 2),
    }
