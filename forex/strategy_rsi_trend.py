"""
forex/strategy_rsi_trend.py
--------------------------
RSI(2) Pullback, REGIME-GATED.  A **SIM-only A/B twin of "rsi"**.

Identical to forex/strategy_rsi.py in every way -- same RSI(2) thresholds,
same 1.5xATR stop, same should_exit, same sizing, same trailing -- EXCEPT
an entry is only taken when ai.regime.classifier.classify_regime() says the
pair is in a matching trend at the signal bar:

    Buy  signal  ->  regime label must be "TRENDING_BULLISH"
    Sell signal  ->  regime label must be "TRENDING_BEARISH"

WHY (2026-09-02, decomposition of an 11y / 49-CORE-pair backtest):
RSI(2)'s raw edge is +0.021 R/trade but UNSTABLE -- ~0 in 2014-2020,
+0.046 in 2021-2026. Broken out by the regime label at entry:

    TRENDING_BULLISH   +0.088 R   (2014-20 +0.083 / 2021-26 +0.092)  STABLE
    TRENDING_BEARISH   +0.040 R   (2014-20 +0.061 / 2021-26 +0.019)  STABLE
    RANGING            +0.011 R   (2014-20 -0.029 / 2021-26 +0.048)  regime-luck
    HIGH_VOLATILITY    -0.013 R

i.e. RSI(2) is only a stable, sizeable edge as "buy the dip IN A TREND".
Gating on TRENDING_* cut the sample to ~17% of signals but lifted avg R to
+0.081-0.089 (positive in BOTH halves), profit factor 1.09 -> 1.37-1.41,
and max drawdown 82 R -> 11-18 R. Net expectancy turned positive at ~EUR150
risk/trade instead of ~EUR600.

**Governance:** this is the "deterministic code" step -- hypothesis from a
decomposition, to be forward-tested on SIM next to the UNTOUCHED "rsi" and
validated with a proper walk-forward before any LIVE consideration.
Deliberately NOT in LIVE_ALLOWED_STRATEGIES / LIVE_EUR_ALLOWED_STRATEGIES.

PURE -- no I/O, no orders, no state (the classifier call is pure too).
Delegates should_exit / size_position / trailing_stop_update straight to
strategy_rsi so the two can never drift.
"""

from __future__ import annotations

import forex.strategy_rsi as _rsi

# ── re-export every constant the runner reads off STRATEGIES["rsi_trend"] ──
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
MIN_BARS       = _rsi.MIN_BARS          # 207 -- classifier needs only 65, fetch is ~500

_TREND_FOR_SIDE = {"Buy": "TRENDING_BULLISH", "Sell": "TRENDING_BEARISH"}

# shared helpers (kept as attributes so existing `strat_rsi._atr(...)` style
# call sites in forex/runner.py work verbatim if ever pointed here)
_rsi_ind = _rsi._rsi
_ema     = _rsi._ema
_atr     = _rsi._atr


def _regime_label(df) -> str:
    """classify_regime's label for this pair, or 'UNKNOWN' on any problem.
    Lazy import (same reasoning as ai/features/trade_proposal.py -- keeps
    module load side-effect-free and avoids a re-entrant import edge when
    runner.py is run as a script then re-imported by safeguard)."""
    try:
        from ai.regime.classifier import classify_regime
        return str(classify_regime(df).get("label") or "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """The plain RSI(2) signals, then kept only if the pair's regime at the
    signal bar is the matching trend. Order (score-sorted) is preserved."""
    base = _rsi.generate_signals(market_data, open_symbols=open_symbols)
    if not base:
        return []
    kept = []
    for sig in base:
        df = market_data.get(sig["symbol"])
        if df is None:
            continue
        label = _regime_label(df)
        if label == _TREND_FOR_SIDE.get(sig["direction"]):
            s = dict(sig)
            s["regime_at_entry"] = label
            kept.append(s)
    return kept


# ── everything else: identical to "rsi", by delegation ──────────────────────
should_exit          = _rsi.should_exit
size_position        = _rsi.size_position
trailing_stop_update = _rsi.trailing_stop_update


def scan_summary(market_data: dict) -> list:
    """rsi's scan summary plus the regime label, so the dashboard/status
    view can show why a pair with an RSI(2) extreme was or wasn't taken."""
    rows = _rsi.scan_summary(market_data) if hasattr(_rsi, "scan_summary") else []
    for row in rows:
        sym = row.get("symbol")
        df = market_data.get(sym) if sym else None
        row["regime"] = _regime_label(df) if df is not None else "UNKNOWN"
        row["regime_gate"] = ("would_trade" if row.get("regime") in _TREND_FOR_SIDE.values()
                              else "filtered")
    return rows
