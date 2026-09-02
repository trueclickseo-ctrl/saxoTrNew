"""
forex/strategy_bb_quality.py
----------------------------
Bollinger-Band mean reversion, NON-DIRECTIONAL-MARKET-GATED.  A **SIM-only
A/B twin of "bb"** (forex/strategy_bb.py).

Identical to forex/strategy_bb.py in every way -- same BB(20,2) + RSI(14)
excursion entry, same 2.0xATR stop, same BB-mid exit / 8-day time stop, same
sizing / trailing -- EXCEPT an entry is only kept when the market is not
strongly directional at the signal bar:

    |plus_di - minus_di|  <=  DI_SPREAD_MAX      (default 14.0)

WHY (2026-09-02, decomposition of a 12y / 49-CORE-pair backtest of "bb"):
"bb"'s raw edge is already stable-positive at +0.048 R/trade (bootstrap 95%
CI [+0.024, +0.073], +0.038 first half / +0.057 second). But it is almost
entirely produced by the signals fired in a BALANCED market:

    |+DI - -DI| <= sample p25   n=1186   +0.219 R   (+0.247 / +0.200)  STABLE
                                          PF 2.07, max drawdown 20 R -> 8 R
    |+DI - -DI| <= sample median n=2371   +0.148 R   (+0.154 / +0.144)  STABLE
    (the top half of DI spread is where "bb" gives most of the edge back)

This is exactly what a mean-reversion strategy should look like: fading a
2-sigma band excursion works when there is no strong trend pushing the "band
walk" further, and loses when +DI/-DI show a real directional move. The base
RSI(14) confirmation and the ADX filter it does NOT have both miss this.
Gating on a low DI spread lifted avg R ~4.5x, profit factor 1.17 -> ~2.0,
positive in BOTH halves, and cut the worst drawdown from -20 R to -8 R.

**Governance:** the "deterministic code" step -- hypothesis from a
decomposition, to be forward-tested on SIM next to the UNTOUCHED "bb" and
validated with a proper walk-forward before any LIVE consideration.
Deliberately NOT in LIVE_ALLOWED_STRATEGIES / LIVE_EUR_ALLOWED_STRATEGIES.

PURE -- no I/O, no orders, no state.  Delegates should_exit / size_position /
trailing_stop_update straight to forex.strategy_bb so the two can never drift.
"""

from __future__ import annotations

import forex.strategy_bb as _bb
from forex.strategy import _adx as _adx_calc   # bb has no ADX/DI of its own

# ── re-export every constant the runner / callers read off the module ────────
BB_PERIOD      = _bb.BB_PERIOD
BB_STD         = _bb.BB_STD
RSI_PERIOD     = _bb.RSI_PERIOD
RSI_OB         = _bb.RSI_OB
RSI_OS         = _bb.RSI_OS
ATR_PERIOD     = _bb.ATR_PERIOD
ATR_STOP_MULT  = _bb.ATR_STOP_MULT
RISK_PCT       = _bb.RISK_PCT
MAX_POSITIONS  = _bb.MAX_POSITIONS
TIME_STOP_DAYS = _bb.TIME_STOP_DAYS
LOT_ROUND      = _bb.LOT_ROUND
MIN_BARS       = _bb.MIN_BARS

# ── the single entry gate (the ONLY new number in this module) ───────────────
DI_SPREAD_MAX = 14.0   # |plus_di - minus_di|; ~ the 12y sample p25 for "bb"

# shared helpers (kept as attributes for `strat_mod._atr(...)` getattr sites)
_bb_ind = _bb._bb
_rsi    = _bb._rsi
_atr    = _bb._atr


def _di_spread(df) -> float:
    """|plus_di - minus_di| at the last bar, or a large number on any problem
    (fail-safe = drop the signal, never accidentally keep one)."""
    try:
        _, pdi, mdi = _adx_calc(df["High"], df["Low"], df["Close"])
        v = abs(float(pdi.iloc[-1]) - float(mdi.iloc[-1]))
        return v if v == v else 999.0   # NaN -> drop
    except Exception:
        return 999.0


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """The plain "bb" signals, then kept only if the market is non-directional
    at the signal bar (|plus_di - minus_di| <= DI_SPREAD_MAX). Score-sorted
    order is preserved."""
    base = _bb.generate_signals(market_data, open_symbols=open_symbols)
    if not base:
        return []
    kept = []
    for sig in base:
        df = market_data.get(sig["symbol"])
        if df is None:
            continue
        di = _di_spread(df)
        if di > DI_SPREAD_MAX:
            continue
        s = dict(sig)
        s["di_spread"] = di
        kept.append(s)
    return kept


# ── everything else: identical to "bb", by delegation ──────────────────────
should_exit          = _bb.should_exit
size_position        = _bb.size_position
trailing_stop_update = _bb.trailing_stop_update


def scan_summary(market_data: dict) -> list:
    """"bb"'s scan summary plus the DI-spread gate value."""
    rows = _bb.scan_summary(market_data) if hasattr(_bb, "scan_summary") else []
    for row in rows:
        sym = row.get("symbol")
        df = market_data.get(sym) if sym else None
        if df is None:
            continue
        di = _di_spread(df)
        row["di_spread"] = di
        row["non_directional"] = bool(di <= DI_SPREAD_MAX)
    return rows
