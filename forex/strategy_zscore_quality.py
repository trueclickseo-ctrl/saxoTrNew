"""
forex/strategy_zscore_quality.py
--------------------------------
Z-Score mean reversion, NON-DIRECTIONAL-MARKET-GATED.  A **SIM-only A/B twin
of "zscore"** (forex/strategy_zscore.py).

Identical to forex/strategy_zscore.py in every way -- same 2.0-sigma entry,
same +/-1% EMA200 macro-zone gate, same 2.5xATR stop, same z-reversion / hard
stop / 12-day exit, same sizing -- EXCEPT an entry is only kept when the
market is not strongly directional at the signal bar:

    |plus_di - minus_di|  <=  DI_SPREAD_MAX      (default 14.0)

WHY (2026-09-02, decomposition of a 12y / 49-CORE-pair backtest of "zscore"):
"zscore"'s raw edge is a coin flip -- +0.002 R/trade, bootstrap 95% CI
[-0.020, +0.024] (spans zero), first half -0.028 / second +0.027. But broken
out by directional conviction it is the same story as "bb":

    |+DI - -DI| bottom quartile   n=690   +0.132 R   (+0.120 / +0.144)  STABLE
    Q2                            n=690   +0.001 R
    Q3                            n=690   +0.002 R
    top quartile                  n=690   -0.128 R   (-0.171 / -0.097)

Fading a 2-sigma excursion works when there is no strong trend pushing price
further away from the mean, and loses when +DI/-DI show a real directional
move. "zscore"'s only filter (close within +/-1% of EMA200) is far too loose
to catch this. Gating on a low DI spread lifted avg R from ~0 to +0.13,
profit factor ~1.0 -> ~1.4, positive in BOTH halves.

(The decomposition also found 19/49 CORE pairs are stable-positive both
halves -- mostly EUR/USD/CHF/AUD/CAD crosses; the losers are the NZD pairs
and GBP-commodity crosses, which are thin and trend-prone. A per-pair
whitelist is a separate, more curve-fit lever -- left to the SIM universe
config, not baked into this module.)

**Governance:** the "deterministic code" step -- hypothesis from a
decomposition, to be forward-tested on SIM next to the UNTOUCHED "zscore" and
validated with a proper walk-forward before any LIVE consideration.
Deliberately NOT in LIVE_ALLOWED_STRATEGIES / LIVE_EUR_ALLOWED_STRATEGIES.

PURE -- no I/O, no orders, no state.  Delegates should_exit / size_position
straight to forex.strategy_zscore so the two can never drift.
"""

from __future__ import annotations

import forex.strategy_zscore as _z
from forex.strategy import _adx as _adx_calc   # zscore has no ADX/DI of its own

# ── re-export every constant the runner / callers read off the module ────────
LOOKBACK       = _z.LOOKBACK
Z_ENTRY        = _z.Z_ENTRY
Z_EXIT         = _z.Z_EXIT
EMA_TREND      = _z.EMA_TREND
ATR_PERIOD     = _z.ATR_PERIOD
ATR_STOP_MULT  = _z.ATR_STOP_MULT
RISK_PCT       = _z.RISK_PCT
TIME_STOP_DAYS = _z.TIME_STOP_DAYS
LOT_ROUND      = _z.LOT_ROUND
MIN_BARS       = _z.MIN_BARS

# ── the single entry gate (the ONLY new number in this module) ───────────────
DI_SPREAD_MAX = 14.0   # |plus_di - minus_di|; ~ the 12y sample p25 (same _adx / pairs as bb_quality)

# shared helpers (kept as attributes for `strat_mod._atr(...)` getattr sites)
_atr     = _z._atr
_zscore  = _z._zscore


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
    """The plain "zscore" signals, then kept only if the market is
    non-directional at the signal bar (|plus_di - minus_di| <= DI_SPREAD_MAX).
    Score-sorted order is preserved."""
    base = _z.generate_signals(market_data, open_symbols=open_symbols)
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


# ── everything else: identical to "zscore", by delegation ───────────────────
should_exit   = _z.should_exit
size_position = _z.size_position


def scan_summary(market_data: dict) -> list:
    rows = _z.scan_summary(market_data) if hasattr(_z, "scan_summary") else []
    for row in rows:
        sym = row.get("symbol")
        df = market_data.get(sym) if sym else None
        if df is None:
            continue
        di = _di_spread(df)
        row["di_spread"] = di
        row["non_directional"] = bool(di <= DI_SPREAD_MAX)
    return rows
