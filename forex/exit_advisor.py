"""
forex/exit_advisor.py -- Stage A of the AI profit-scan for open positions.

A DETERMINISTIC give-back-risk scorer. For each open position it looks at
how much unrealised profit is at risk of being surrendered and returns
one of HOLD / TIGHTEN / EXIT with the reasons.

Stage A (this file, 2026-08-31): pure rules, no ML. Runs in SHADOW MODE
only -- forex/runner.py logs what it WOULD do every exits-check cycle
(forward_observation.log_exit_advisor_shadow) and never acts on it. Weeks
of that shadow record, joined against the real exit outcome per trade,
tell us whether it beats the plain ladder / RSI-recovery exits.

Stage B (later): train a classifier on the accumulated observation cards
using these exact features.
Stage C: the design doc -- docs/atos_exit_advisor.md.

THIS MODULE IS PURE -- no I/O, no orders, no state, no network.
"""

from __future__ import annotations

import math

import forex.strategy_rsi as _rsi_mod

# Score bands -> recommendation. Deliberately conservative: EXIT only on a
# strong give-back signal, since the primary RSI-recovery / TP / time-stop
# exits are still doing their job underneath.
_TIGHTEN_AT = 45
_EXIT_AT    = 70


def _atr_series(df):
    return _rsi_mod._atr(df["High"], df["Low"], df["Close"])


def score(pos: dict, df, strat_name: str) -> dict | None:
    """Give-back-risk score for one open position given current daily bars.

    Returns a dict {score 0-100, recommendation, signals{...}, r_now,
    mfe_r} or None when there isn't enough to judge (no df, no R
    reference, flat/underwater position).
    """
    if df is None or len(df) < _rsi_mod.RSI_PERIOD + 2:
        return None

    is_long = pos.get("direction", "Buy") == "Buy"
    entry   = float(pos.get("entry_price", 0) or 0)
    if entry <= 0:
        return None

    init_stop = pos.get("initial_stop_price")
    if init_stop:
        R = abs(entry - float(init_stop))
    else:
        atr_entry = float(pos.get("atr_at_entry", 0) or 0)
        R = _rsi_mod.ATR_STOP_MULT * atr_entry
    if R <= 0:
        return None

    close = float(df["Close"].iloc[-1])
    r_now = (close - entry) / R if is_long else (entry - close) / R
    if r_now <= 0:
        # Not in profit -> nothing to protect; the hard stop owns this.
        return None

    # --- features -----------------------------------------------------------
    mae_eur = pos.get("mae_eur")
    mfe_eur = pos.get("mfe_eur")
    risk_eur = pos.get("risk_eur_at_entry")
    mfe_r = (mfe_eur / risk_eur) if (mfe_eur and risk_eur and risk_eur > 0) else r_now
    mfe_r = max(mfe_r, r_now)

    # give-back: how far price has retraced from its best, as a fraction of MFE
    giveback_frac = (mfe_r - r_now) / mfe_r if mfe_r > 0 else 0.0

    try:
        atr_now = float(_atr_series(df).iloc[-1])
        if math.isnan(atr_now):
            atr_now = 0.0
    except Exception:
        atr_now = 0.0
    atr_entry = float(pos.get("atr_at_entry", 0) or 0)
    atr_expansion = (atr_now / atr_entry) if atr_entry > 0 else 1.0

    try:
        rsi_now = float(_rsi_mod._rsi(df["Close"]).iloc[-1])
        if math.isnan(rsi_now):
            rsi_now = 50.0
    except Exception:
        rsi_now = 50.0
    # For a long, RSI marching toward RSI_EXIT_LONG means the strategy's own
    # recovery exit is near -- LOW give-back risk. RSI stalled in the middle
    # while price gave back is the bad combination.
    rsi_toward_exit = (rsi_now >= _rsi_mod.RSI_EXIT_LONG) if is_long else (rsi_now <= _rsi_mod.RSI_EXIT_SHORT)

    days_held = 0
    try:
        from datetime import date
        days_held = (date.today() - date.fromisoformat(pos.get("entry_date", ""))).days
    except Exception:
        pass
    time_frac = days_held / _rsi_mod.TIME_STOP_DAYS if _rsi_mod.TIME_STOP_DAYS else 0.0

    cur_stop = float(pos.get("stop_price", 0) or 0)
    dist_to_stop_r = ((close - cur_stop) if is_long else (cur_stop - close)) / R if R > 0 else 9.9

    # --- scoring (0 = safe hold, 100 = get out) -----------------------------
    s = 0.0
    signals = {}

    # 1. Give-back from the peak -- the dominant term.
    gb_pts = min(50.0, giveback_frac * 90.0)      # 55% retrace -> ~50 pts
    s += gb_pts
    signals["giveback_frac"] = round(giveback_frac, 2)

    # 2. Was ever meaningfully in profit (only worth protecting if so).
    if mfe_r >= 1.0:
        s += 10.0
    signals["mfe_r"] = round(mfe_r, 2)

    # 3. Volatility expanding while giving back -> exits get worse fast.
    if atr_expansion > 1.25 and giveback_frac > 0.25:
        s += 15.0
    signals["atr_expansion"] = round(atr_expansion, 2)

    # 4. RSI NOT heading to its own exit while price fades -> ladder/recovery
    #    won't save it soon.
    if not rsi_toward_exit and giveback_frac > 0.3:
        s += 15.0
    signals["rsi_now"] = round(rsi_now, 1)
    signals["rsi_toward_exit"] = rsi_toward_exit

    # 5. Late in the trade's life.
    if time_frac > 0.6:
        s += 10.0
    signals["time_frac"] = round(time_frac, 2)

    # 6. Stop already very close -> the ladder has this; lower the urgency.
    if dist_to_stop_r < 0.15:
        s -= 15.0
    signals["dist_to_stop_r"] = round(dist_to_stop_r, 2)

    s = max(0.0, min(100.0, s))
    rec = "EXIT" if s >= _EXIT_AT else "TIGHTEN" if s >= _TIGHTEN_AT else "HOLD"

    return {
        "score": round(s, 1),
        "recommendation": rec,
        "r_now": round(r_now, 2),
        "mfe_r": round(mfe_r, 2),
        "signals": signals,
    }
