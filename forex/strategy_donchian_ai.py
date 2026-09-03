"""
forex/strategy_donchian_ai.py
------------------------------
SIM-ONLY — A/B twin of "donchian" with AI-learnable quality filters.

The plain Donchian fires on every 30-day breakout regardless of regime or
directional momentum.  Historical decomposition shows the edge lives entirely
in trending, high-DI-spread environments; ranging and low-spread entries drag
the aggregate into break-even territory.

This strategy keeps the same Donchian breakout kernel but gates every signal
through three deterministic filters that the AI Research Analyst can refine
over time (via ai/research/decompose.py → hypothesis backlog → human review):

  1. DI-SPREAD gate: |+DI(14) − −DI(14)| ≥ DI_SPREAD_MIN (18)
     Confirmed directional momentum — same gate as bb_quality / zscore_quality.
     Blocks false breakouts in low-momentum, choppy tape.

  2. REGIME gate: classify_regime() must return TRENDING_BULLISH (long) or
     TRENDING_BEARISH (short) — same gate as rsi_trend.
     Blocks breakouts that fire in statistically RANGING regimes.

  3. ATR-PERCENTILE gate: today's ATR must be above the rolling median
     ATR over the past ATR_LOOKBACK bars.  Elevated volatility = real
     breakout; compressed volatility = likely noise, not a channel-clearing move.

ENTRY (all five must be true — donchian logic + the three AI gates):
  Long:  close > 30-day high  AND  EMA200 trend  AND  ADX ≥ 25
         AND  DI-spread ≥ 18  AND  regime=TRENDING_BULLISH  AND  ATR > median-ATR

SHORT logic symmetric.

EXIT: identical to donchian (15-day trailing channel + 2×ATR hard stop + 30d time).

Pure module — no I/O, no orders, no state.
Regime classifier call is lazy-imported and best-effort: any failure degrades to
the unfiltered Donchian signal (same as what _would_ have been taken without the AI gate).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import forex.strategy_donchian as _don  # reuse exit / sizing / scan helpers

# ── Tunable thresholds (Research Analyst adjusts these via hypothesis→backtest) ──

BREAKOUT_PERIOD  = _don.BREAKOUT_PERIOD  # 30
EXIT_PERIOD      = _don.EXIT_PERIOD      # 15
ATR_PERIOD       = _don.ATR_PERIOD       # 14
EMA_TREND        = _don.EMA_TREND        # 200
ADX_PERIOD       = _don.ADX_PERIOD       # 14
ADX_MIN          = _don.ADX_MIN          # 25
ATR_STOP_MULT    = _don.ATR_STOP_MULT    # 2.0
RISK_PCT         = _don.RISK_PCT         # 0.0025
MAX_POSITIONS    = 4                     # same cap as donchian
TIME_STOP_DAYS   = _don.TIME_STOP_DAYS   # 30
LOT_ROUND        = _don.LOT_ROUND        # 1_000
MIN_BARS         = _don.MIN_BARS

DI_SPREAD_MIN    = 18    # |+DI − −DI| threshold; hypothesis: ≤14 → near-zero edge
ATR_LOOKBACK     = 50    # rolling window for ATR-percentile gate
TRENDING_LABELS  = frozenset({"TRENDING_BULLISH", "TRENDING_BEARISH"})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _di_spread(h: pd.Series, l: pd.Series, c: pd.Series,
               period: int = ADX_PERIOD) -> float:
    """Return |+DI − −DI| for the last bar.  Returns None on failure."""
    try:
        prev_c = c.shift(1); prev_h = h.shift(1); prev_l = l.shift(1)
        tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        dm_plus  = ((h - prev_h).clip(lower=0)).where(h - prev_h > prev_l - l, 0)
        dm_minus = ((prev_l - l).clip(lower=0)).where(prev_l - l > h - prev_h, 0)
        atr14    = tr.ewm(alpha=1/period, adjust=False, min_periods=period).mean()
        di_plus  = (100 * dm_plus.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr14).iloc[-1]
        di_minus = (100 * dm_minus.ewm(alpha=1/period, adjust=False, min_periods=period).mean() / atr14).iloc[-1]
        return float(abs(di_plus - di_minus))
    except Exception:
        return None


def _regime_label(df: pd.DataFrame) -> str:
    """classify_regime label, or 'UNKNOWN' on any failure."""
    try:
        from ai.regime.classifier import classify_regime
        return str(classify_regime(df).get("label") or "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def _atr_above_median(h: pd.Series, l: pd.Series, c: pd.Series) -> bool | None:
    """True if today's ATR(14) is above the rolling median of last ATR_LOOKBACK bars."""
    try:
        prev = c.shift(1)
        tr   = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
        atr  = tr.ewm(alpha=1/ATR_PERIOD, adjust=False, min_periods=ATR_PERIOD).mean()
        recent = atr.dropna().iloc[-ATR_LOOKBACK:]
        if len(recent) < 20:
            return None
        return float(atr.iloc[-1]) >= float(recent.median())
    except Exception:
        return None


# ── Signal generation ─────────────────────────────────────────────────────────

def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Donchian breakout signals filtered by DI-spread + regime + ATR-percentile."""
    if open_symbols is None:
        open_symbols = set()

    signals = []
    for sym, df in market_data.items():
        if sym in open_symbols:
            continue
        if df is None or len(df) < MIN_BARS:
            continue

        h, l, c = df["High"], df["Low"], df["Close"]
        today    = float(c.iloc[-1])
        history  = c.iloc[-(BREAKOUT_PERIOD + 1):-1]
        high30   = float(history.max())
        low30    = float(history.min())

        from forex.strategy_donchian import _atr, _adx
        atr_val  = float(_atr(h, l, c).iloc[-1])
        ema200   = float(c.ewm(span=EMA_TREND, adjust=False).mean().iloc[-1])
        adx_val  = float(_adx(h, l, c).iloc[-1])

        if np.isnan(high30) or np.isnan(low30) or atr_val <= 0:
            continue
        if np.isnan(ema200) or np.isnan(adx_val):
            continue
        if adx_val < ADX_MIN:
            continue

        # ── Determine direction ────────────────────────────────────────────
        is_long  = today > high30 and today > ema200
        is_short = today < low30  and today < ema200
        if not (is_long or is_short):
            continue

        # ── Gate 1: DI-spread ─────────────────────────────────────────────
        di_spread = _di_spread(h, l, c)
        if di_spread is None or di_spread < DI_SPREAD_MIN:
            continue

        # ── Gate 2: regime ────────────────────────────────────────────────
        regime = _regime_label(df)
        if regime not in ("UNKNOWN",):  # only filter if classifier responded
            if is_long  and regime != "TRENDING_BULLISH":
                continue
            if is_short and regime != "TRENDING_BEARISH":
                continue

        # ── Gate 3: ATR above rolling median ──────────────────────────────
        atr_ok = _atr_above_median(h, l, c)
        if atr_ok is False:            # False = below median; None = degraded, allow through
            continue

        if is_long:
            stop  = today - ATR_STOP_MULT * atr_val
            score = (today - high30) / atr_val
            signals.append({
                "symbol":         sym,
                "direction":      "Buy",
                "score":          float(score),
                "atr":            float(atr_val),
                "close":          today,
                "stop_price":     float(stop),
                "breakout_level": float(high30),
                "adx":            float(adx_val),
                "di_spread":      float(di_spread),
                "regime":         regime,
                "atr_above_med":  bool(atr_ok) if atr_ok is not None else None,
            })
        else:
            stop  = today + ATR_STOP_MULT * atr_val
            score = (low30 - today) / atr_val
            signals.append({
                "symbol":         sym,
                "direction":      "Sell",
                "score":          float(score),
                "atr":            float(atr_val),
                "close":          today,
                "stop_price":     float(stop),
                "breakout_level": float(low30),
                "adx":            float(adx_val),
                "di_spread":      float(di_spread),
                "regime":         regime,
                "atr_above_med":  bool(atr_ok) if atr_ok is not None else None,
            })

    signals.sort(key=lambda x: x["score"], reverse=True)
    return signals


# ── Exit / sizing / scan_summary delegate to donchian ─────────────────────────

def should_exit(position: dict, df: pd.DataFrame,
                calendar_days_held: int) -> tuple:
    return _don.should_exit(position, df, calendar_days_held)


def size_position(account_equity: float, atr: float,
                  min_units: float = 1_000,
                  block_below_min: bool = False) -> int:
    return _don.size_position(account_equity, atr, min_units, block_below_min)


def trailing_stop_update(current_stop: float, current_price: float,
                         current_atr: float, direction: str = "Buy") -> float:
    return _don.trailing_stop_update(current_stop, current_price, current_atr, direction)


def scan_summary(market_data: dict) -> list:
    return _don.scan_summary(market_data)
