"""
forex/strategy_zscore_quality_tb.py
-------------------------------------
zscore_quality + TRENDING_BULLISH regime gate.  A **SIM-only A/B twin of
"zscore_quality"** (forex/strategy_zscore_quality.py).

Identical to zscore_quality in every way -- same 2-sigma entry, same DI-spread
non-directional gate (|+DI − −DI| <= 14), same 2.5xATR stop, same z-reversion
/ hard-stop / 12-day exit, same sizing -- EXCEPT an entry is only kept when the
AI regime classifier labels the pair TRENDING_BULLISH at the signal bar.

WHY (2026-09-04, AI Research Analyst H20260904-c9e606, decomposition gate):
zscore_quality base avg_r is +0.004 (flat, commission-dominated).  Broken out
by regime at entry:

    TRENDING_BULLISH  n=54  avg_r +0.150  PF 2.23
                            1st half +0.231 / 2nd half +0.119  (STABLE)

The intuition: z-score mean-reversion pullbacks work best with a mild
directional tailwind -- the trend provides a natural attractor that pulls price
back to the mean after an excursion, rather than the ranging/chop context where
the excursion can walk further without a structural bias to reverse.

Note: the hypothesis that zscore_quality should be restricted to RANGING
(its nominal design) failed the gate -- no bucket positive in both halves.
TRENDING_BULLISH is the empirical winner, not the prior.

**Governance:** H20260904-c9e606 status = backtesting.  "zscore_quality" is
untouched.  Never in LIVE_ALLOWED_STRATEGIES / LIVE_EUR_ALLOWED_STRATEGIES.

PURE -- no I/O, no orders, no state.  Exit / sizing delegate to zscore_quality
which in turn delegates to forex.strategy_zscore.
"""

from __future__ import annotations

import forex.strategy_zscore_quality as _zq

# ── re-export every constant the runner / callers read off the module ────────
LOOKBACK       = _zq.LOOKBACK
Z_ENTRY        = _zq.Z_ENTRY
Z_EXIT         = _zq.Z_EXIT
EMA_TREND      = _zq.EMA_TREND
ATR_PERIOD     = _zq.ATR_PERIOD
ATR_STOP_MULT  = _zq.ATR_STOP_MULT
RISK_PCT       = _zq.RISK_PCT
TIME_STOP_DAYS = _zq.TIME_STOP_DAYS
LOT_ROUND      = _zq.LOT_ROUND
MIN_BARS       = _zq.MIN_BARS
DI_SPREAD_MAX  = _zq.DI_SPREAD_MAX

# shared helpers (for strat_mod._atr(...) style getattr sites in runner.py)
_atr    = _zq._atr
_zscore = _zq._zscore

# ── the single additional gate ───────────────────────────────────────────────
_REQUIRED_REGIME = "TRENDING_BULLISH"


def _regime_label(df) -> str:
    """classify_regime label for this pair, or 'UNKNOWN' on any problem.
    Lazy import keeps module load side-effect-free."""
    try:
        from ai.regime.classifier import classify_regime
        return str(classify_regime(df).get("label") or "UNKNOWN")
    except Exception:
        return "UNKNOWN"


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """zscore_quality signals, then kept only if regime == TRENDING_BULLISH.
    Score-sorted order is preserved."""
    base = _zq.generate_signals(market_data, open_symbols=open_symbols)
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


# ── everything else: identical to zscore_quality, by delegation ─────────────
should_exit   = _zq.should_exit
size_position = _zq.size_position


def scan_summary(market_data: dict) -> list:
    """zscore_quality's scan summary plus the regime label."""
    rows = _zq.scan_summary(market_data) if hasattr(_zq, "scan_summary") else []
    for row in rows:
        sym = row.get("symbol")
        df = market_data.get(sym) if sym else None
        label = _regime_label(df) if df is not None else "UNKNOWN"
        row["regime"] = label
        row["tb_gate"] = ("would_trade" if label == _REQUIRED_REGIME else "filtered")
    return rows
