"""
forex/strategy_bb_quality_hv.py
--------------------------------
bb_quality + HIGH_VOLATILITY regime gate.  A **SIM-only A/B twin of
"bb_quality"** (forex/strategy_bb_quality.py).

Identical to bb_quality in every way -- same BB(20,2) + RSI(14) excursion
entry, same DI-spread non-directional gate (|+DI − −DI| <= 14), same 2.0xATR
stop, same BB-mid exit / 8-day time stop, same sizing / trailing -- EXCEPT an
entry is only kept when the AI regime classifier labels the pair HIGH_VOLATILITY
at the signal bar.

WHY (2026-09-04, AI Research Analyst H20260904-37779e, decomposition gate):
bb_quality's base avg_r is +0.024 (near-flat, commission-dominated).  But the
HIGH_VOLATILITY regime bucket runs:

    HIGH_VOLATILITY  n=33   avg_r +0.247   PF 2.70
                            1st half +0.215 / 2nd half +0.348  (STABLE)

bb_quality is still SIM-only, meaning this twin is SIM-only too.  The original
"bb_quality" is untouched.  This is purely an A/B forward test of the
hypothesis that bb mean-reversion captures the best moves specifically when
the market is making large swings rather than just sitting in a range.

Governance: H20260904-37779e status = backtesting.  Never in
LIVE_ALLOWED_STRATEGIES / LIVE_EUR_ALLOWED_STRATEGIES.

PURE -- no I/O, no orders, no state.  Exit / sizing / trailing delegate to
bb_quality which in turn delegates to forex.strategy_bb.
"""

from __future__ import annotations

import forex.strategy_bb_quality as _bbq

# ── re-export every constant the runner / callers read off the module ────────
BB_PERIOD      = _bbq.BB_PERIOD
BB_STD         = _bbq.BB_STD
RSI_PERIOD     = _bbq.RSI_PERIOD
RSI_OB         = _bbq.RSI_OB
RSI_OS         = _bbq.RSI_OS
ATR_PERIOD     = _bbq.ATR_PERIOD
ATR_STOP_MULT  = _bbq.ATR_STOP_MULT
RISK_PCT       = _bbq.RISK_PCT
MAX_POSITIONS  = _bbq.MAX_POSITIONS
TIME_STOP_DAYS = _bbq.TIME_STOP_DAYS
LOT_ROUND      = _bbq.LOT_ROUND
MIN_BARS       = _bbq.MIN_BARS
DI_SPREAD_MAX  = _bbq.DI_SPREAD_MAX

# shared helpers (for strat_mod._atr(...) style getattr sites in runner.py)
_bb_ind = _bbq._bb_ind
_rsi    = _bbq._rsi
_atr    = _bbq._atr

# ── the single additional gate ───────────────────────────────────────────────
_REQUIRED_REGIME = "HIGH_VOLATILITY"


def _regime_label(df) -> str:
    """classify_regime label for this pair, or 'UNKNOWN' on any problem.
    Lazy import keeps module load side-effect-free."""
    try:
        from ai.regime.classifier import classify_regime
        return str(classify_regime(df).get("label") or "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """bb_quality signals, then kept only if regime == HIGH_VOLATILITY.
    Score-sorted order is preserved."""
    base = _bbq.generate_signals(market_data, open_symbols=open_symbols)
    if not base:
        return []
    kept = []
    for sig in base:
        df = market_data.get(sig["symbol"])
        if df is None:
            continue
        label = _regime_label(df)
        if label != _REQUIRED_REGIME:
            continue
        s = dict(sig)
        s["regime_at_entry"] = label
        kept.append(s)
    return kept


# ── everything else: identical to bb_quality, by delegation ─────────────────
should_exit          = _bbq.should_exit
size_position        = _bbq.size_position
trailing_stop_update = _bbq.trailing_stop_update


def scan_summary(market_data: dict) -> list:
    """bb_quality's scan summary plus the regime label."""
    rows = _bbq.scan_summary(market_data) if hasattr(_bbq, "scan_summary") else []
    for row in rows:
        sym = row.get("symbol")
        df = market_data.get(sym) if sym else None
        label = _regime_label(df) if df is not None else "UNKNOWN"
        row["regime"] = label
        row["hv_gate"] = ("would_trade" if label == _REQUIRED_REGIME else "filtered")
    return rows
