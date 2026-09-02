"""
forex/strategy_ema_trend.py
---------------------------
EMA(5/30) crossover, CLEAN-CROSSOVER-GATED.  A **SIM-only A/B twin of "ema"**
(forex/strategy.py).

Identical to forex/strategy.py in every way -- same EMA(5/30) crossover, same
ADX>=25 filter, same DI-alignment requirement, same 1.5xATR stop, same
should_exit / sizing / trailing -- EXCEPT an entry is only kept when the
crossover is BOTH fresh and backed by real directional conviction:

    crossover age   <= MAX_CROSSOVER_AGE bars   (default 3, base allows 15)
    |plus_di - minus_di|  >=  DI_SPREAD_MIN      (default 15.0)

WHY (2026-09-02, decomposition of a 12y / 49-CORE-pair backtest of "ema"):
"ema"'s raw edge is +0.036 R/trade and UNSTABLE (+0.064 first half / +0.010
second half, bootstrap 95% CI spans zero). Broken out by entry context, the
edge concentrates entirely in the fresh + high-conviction crossovers:

    fresh crossover (age<=3)        +0.103 R   (+0.163 / +0.056)   STABLE
    DI spread >= sample median     +0.110 R   (+0.120 / +0.099)   STABLE
    BOTH (fresh + DI)      n=242    +0.298 R   (+0.356 / +0.250)   STABLE
                                     PF 1.97, max drawdown 47 R -> 6 R

i.e. "ema" only has a stable, sizeable edge on the crossovers that are still
young AND show a clear +DI/-DI gap. A stale crossover (price already ran) or a
marginal DI gap (the "scissors" chop the ADX filter alone doesn't catch) is
where the base strategy bleeds. Gating on both lifted avg R ~8x, profit factor
1.09 -> ~2.0, positive in BOTH halves, and cut the worst peak-to-trough
drawdown from -47 R to -6 R.

**Governance:** the "deterministic code" step -- hypothesis from a
decomposition, to be forward-tested on SIM next to the UNTOUCHED "ema" and
validated with a proper walk-forward before any LIVE consideration.
Deliberately NOT in LIVE_ALLOWED_STRATEGIES / LIVE_EUR_ALLOWED_STRATEGIES.

PURE -- no I/O, no orders, no state.  Delegates should_exit / size_position /
trailing_stop_update straight to forex.strategy so the two can never drift.
"""

from __future__ import annotations

import forex.strategy as _ema

# ── re-export every constant the runner / callers read off the module ────────
FAST_EMA       = _ema.FAST_EMA
SLOW_EMA       = _ema.SLOW_EMA
ADX_PERIOD     = _ema.ADX_PERIOD
ADX_MIN        = _ema.ADX_MIN
ATR_PERIOD     = _ema.ATR_PERIOD
ATR_STOP_MULT  = _ema.ATR_STOP_MULT
RISK_PCT       = _ema.RISK_PCT
MAX_POSITIONS  = _ema.MAX_POSITIONS
TIME_STOP_DAYS = _ema.TIME_STOP_DAYS
LOT_ROUND      = _ema.LOT_ROUND
MIN_BARS       = _ema.MIN_BARS

# ── the two entry gates (the ONLY new numbers in this module) ────────────────
MAX_CROSSOVER_AGE = 3      # bars; base scans back up to _BASE_SIGNAL_LOOKBACK (15)
DI_SPREAD_MIN     = 15.0   # |plus_di - minus_di|; ~ the 12y sample median for "ema"
_BASE_SIGNAL_LOOKBACK = 15  # must match the local SIGNAL_LOOKBACK in _ema.generate_signals

# shared helpers (kept as attributes so `strat_mod._atr(...)` getattr call
# sites in forex/runner.py resolve if ever pointed here)
_ema_ind = _ema._ema
_atr     = _ema._atr
_adx     = _ema._adx


def _crossover_age(df) -> int | None:
    """Bars since the EMA(5/30) crossover, replicating the exact back-scan in
    _ema.generate_signals (first crossover found wins, <= _BASE_SIGNAL_LOOKBACK).
    Returns None if no crossover in the window."""
    c = df["Close"]
    fast = _ema._ema(c, FAST_EMA)
    slow = _ema._ema(c, SLOW_EMA)
    n = len(fast)
    for k in range(1, min(_BASE_SIGNAL_LOOKBACK + 1, n - 1)):
        f_cur, f_prev = float(fast.iloc[-k]), float(fast.iloc[-(k + 1)])
        s_cur, s_prev = float(slow.iloc[-k]), float(slow.iloc[-(k + 1)])
        if (f_prev <= s_prev and f_cur > s_cur) or (f_prev >= s_prev and f_cur < s_cur):
            return k
    return None


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """The plain "ema" signals, then kept only if the crossover is fresh
    (age <= MAX_CROSSOVER_AGE) AND directional conviction is real
    (|plus_di - minus_di| >= DI_SPREAD_MIN). Score-sorted order is preserved."""
    base = _ema.generate_signals(market_data, open_symbols=open_symbols)
    if not base:
        return []
    kept = []
    for sig in base:
        df = market_data.get(sig["symbol"])
        if df is None:
            continue
        di_spread = abs(float(sig.get("plus_di", 0.0)) - float(sig.get("minus_di", 0.0)))
        if di_spread < DI_SPREAD_MIN:
            continue
        age = _crossover_age(df)
        if age is None or age > MAX_CROSSOVER_AGE:
            continue
        s = dict(sig)
        s["crossover_age"] = age
        s["di_spread"] = di_spread
        kept.append(s)
    return kept


# ── everything else: identical to "ema", by delegation ──────────────────────
should_exit          = _ema.should_exit
size_position        = _ema.size_position
trailing_stop_update = _ema.trailing_stop_update


def scan_summary(market_data: dict) -> list:
    """"ema"'s scan summary plus the two gate values, so the dashboard/status
    view can show why a crossover was or wasn't taken."""
    rows = _ema.scan_summary(market_data) if hasattr(_ema, "scan_summary") else []
    for row in rows:
        sym = row.get("symbol")
        df = market_data.get(sym) if sym else None
        if df is None:
            continue
        di = abs(float(row.get("plus_di", 0.0)) - float(row.get("minus_di", 0.0)))
        age = _crossover_age(df)
        row["di_spread"] = di
        row["crossover_age"] = age
        row["clean_crossover"] = bool(
            age is not None and age <= MAX_CROSSOVER_AGE and di >= DI_SPREAD_MIN)
    return rows
