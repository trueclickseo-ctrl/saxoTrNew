"""
forex/strategy_rsi_atr.py
--------------------------
RSI(2) Pullback, HIGH-VOLATILITY-FOCUSED.  A **SIM-only A/B twin of "rsi"**.

Identical to forex/strategy_rsi.py in every way -- same RSI(2) thresholds,
same 1.5xATR stop, same should_exit, same sizing, same trailing -- EXCEPT
an entry is only taken when the current ATR(14) reading is in the top 34% of
its trailing 252-bar (1y) distribution at the signal bar.

WHY (2026-09-03, AI Research Analyst first run, H20260903-74e4df):
Decomposition of 7,152 RSI trades over 13y / 49-pair universe by ATR
percentile at entry:

    low  ≤ 0.33   avg_r −0.005  WR 62.7%  PF 0.98  CI [−0.025, +0.016]  FAIL
    mid           avg_r +0.011  WR 63.3%  PF 1.05  CI [−0.016, +0.039]  FAIL
    high > 0.66   avg_r +0.051  WR 65.7%  PF 1.25  CI [+0.028, +0.074]  PASS

The "high" bucket is stable: H1 +0.029, H2 +0.074, CI firmly excludes zero.
Low-vol entries (bottom third) drag the aggregate RSI result negative.
Restricting to high-vol entries concentrates on the 2,268/7,152 trades that
actually earn (avg_r 3x the base).

Note: the AI framed the hypothesis as "exit early in high vol (give-back)".
The decomposition inverted the causal story: high-vol entries are BETTER,
not worse. The action is a FOCUS filter (only take these trades), not an
early exit. This twin tests the focus thesis.

**Governance:** SIM-only isolated A/B twin. Not in LIVE_ALLOWED_STRATEGIES
or LIVE_EUR_ALLOWED_STRATEGIES. Forward-test alongside the untouched "rsi"
twin; promote only after walk-forward review. PURE -- no I/O, no orders,
no state.
"""

from __future__ import annotations

import numpy as np

import forex.strategy_rsi as _rsi

# ── re-export every constant the runner reads off STRATEGIES["rsi_atr"] ──
RSI_PERIOD     = _rsi.RSI_PERIOD
RSI_OVERSOLD   = _rsi.RSI_OVERSOLD
RSI_OVERBOUGHT = _rsi.RSI_OVERBOUGHT
RSI_EXIT_LONG  = _rsi.RSI_EXIT_LONG
RSI_EXIT_SHORT = _rsi.RSI_EXIT_SHORT
TREND_EMA      = _rsi.TREND_EMA
ATR_PERIOD     = _rsi.ATR_PERIOD
ATR_STOP_MULT  = _rsi.ATR_STOP_MULT
RISK_PCT       = _rsi.RISK_PCT
MAX_POSITIONS  = _rsi.MAX_POSITIONS
TIME_STOP_DAYS = _rsi.TIME_STOP_DAYS
LOT_ROUND      = _rsi.LOT_ROUND
MIN_BARS       = _rsi.MIN_BARS

# Decomposition gate: top 34% of ATR readings (1y rolling window).
_ATR_PCTILE_GATE    = 0.66
_ATR_PCTILE_WINDOW  = 252
_ATR_PCTILE_MINBARS = 60


def _cur_atr_pctile(highs, lows, closes) -> float:
    """Fraction of trailing 252-bar ATR readings <= today's ATR.
    Returns 0.0 when there is insufficient history (<60 bars)."""
    atr_s = _rsi._atr(highs, lows, closes)
    valid = atr_s.dropna()
    window = valid.iloc[-_ATR_PCTILE_WINDOW:]
    if len(window) < _ATR_PCTILE_MINBARS:
        return 0.0
    cur = float(window.iloc[-1])
    return float((window <= cur).sum() / len(window))


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """Plain RSI(2) signals, kept only when atr_pctile > 0.66 at entry."""
    base = _rsi.generate_signals(market_data, open_symbols=open_symbols)
    if not base:
        return []
    kept = []
    for sig in base:
        df = market_data.get(sig["symbol"])
        if df is None:
            continue
        pctile = _cur_atr_pctile(df["High"], df["Low"], df["Close"])
        if pctile > _ATR_PCTILE_GATE:
            s = dict(sig)
            s["atr_pctile"] = round(pctile, 3)
            kept.append(s)
    return kept


# ── everything else: identical to "rsi", by delegation ──────────────────────
should_exit          = _rsi.should_exit
size_position        = _rsi.size_position
trailing_stop_update = _rsi.trailing_stop_update
