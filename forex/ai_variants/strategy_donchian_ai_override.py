# AI-WRITTEN Phase 2+3 2025-06-16 by claude-sonnet-5
# Entry filter: Correlated JPY-cross basket (HKDJPY/JPYHKD/USDJPY) group cooldown of 12h to block simultaneous correlated stack-ups.
# Exit filter: Require 2 consecutive daily closes beyond stop_price before confirming hard_stop exit -- unchanged; latest ledger (30 trades) shows the filter is working (53.3% WR, +0.6 avg_pnl, up from 30.4%/-1.1R), so no new exit-reason pattern warrants further change.

from __future__ import annotations

import pandas as pd

import forex.strategy_donchian_ai as _orig
from forex.strategy_donchian_ai import should_exit as _orig_should_exit

# Correlated JPY-cross basket identified in the closed-trade ledger: these
# symbols repeatedly fired signals within seconds of one another and were
# then stopped out together on the same underlying JPY move, turning a
# single adverse move into 3 simultaneous correlated losses instead of 1.
CORRELATED_GROUPS = [
    frozenset({"HKDJPY", "JPYHKD", "USDJPY"}),
]

# Minimum hours between accepted signals within the same correlated group.
GROUP_COOLDOWN_HOURS = 12.0

# Module-level state: last bar-timestamp a signal was accepted for a group.
_last_group_signal_ts: dict = {}


def _bar_time(df):
    """Best-effort extraction of the current bar's timestamp."""
    try:
        ts = df.index[-1]
        if isinstance(ts, pd.Timestamp):
            return ts
        return pd.Timestamp(ts)
    except Exception:
        return None


def _get_symbol(sig):
    if isinstance(sig, dict):
        return sig.get("symbol") or sig.get("Symbol")
    return getattr(sig, "symbol", None)


def _group_for_symbol(sym):
    for grp in CORRELATED_GROUPS:
        if sym in grp:
            return grp
    return None


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wraps donchian_ai.generate_signals with a correlated-basket cooldown.

    Evidence: the closed-trade ledger for donchian_ai showed HKDJPY, JPYHKD
    and USDJPY signals firing within seconds of each other repeatedly (e.g.
    21:34:13 / 21:34:17 / 21:34:22, 20:38:24 / 20:38:27 / 20:38:33,
    20:07:41 / 20:07:45) and then all exiting via hard_stop within minutes
    of each other -- a single correlated JPY move producing 3 simultaneous
    losses instead of 1. The existing per-symbol cooldown does not block
    this because the symbols differ. This wrapper adds a group-level
    cooldown: once any symbol in a correlated basket fires a signal, no
    other symbol in that same basket may fire again for
    GROUP_COOLDOWN_HOURS.
    """
    signals = _orig.generate_signals(market_data, open_symbols=open_symbols, **kwargs)
    if not signals:
        return signals

    filtered = []
    for sig in signals:
        sym = _get_symbol(sig)
        grp = _group_for_symbol(sym) if sym else None
        if grp is None:
            filtered.append(sig)
            continue

        df = market_data.get(sym)
        now_ts = _bar_time(df) if df is not None else None

        last_ts = _last_group_signal_ts.get(grp)
        if now_ts is not None and last_ts is not None:
            hours_since = (now_ts - last_ts).total_seconds() / 3600.0
            if hours_since < GROUP_COOLDOWN_HOURS:
                continue  # block: another symbol in this correlated group fired recently

        filtered.append(sig)
        if now_ts is not None:
            _last_group_signal_ts[grp] = now_ts

    return filtered


# ── Exit filter (Phase 3) ─────────────────────────────────────────────────────────────
#
# Evidence (latest ledger, 30 quality trades): 100% of closed trades still
# exit via hard_stop, but the distribution has shifted materially since the
# filter's original justification -- win rate is now 53.3% (up from 30.4%)
# and avg_pnl is +0.6R (up from -1.1R). This is consistent with the
# 2-consecutive-close confirmation successfully filtering out single-bar
# wick/whipsaw stop violations while leaving genuine directional stop-outs
# intact. There is still zero trend_break or time_stop representation in
# the ledger, so there is no new exit-reason pattern to target. Per the
# "no clear pattern to add" rule, the exit logic is left UNCHANGED rather
# than layering on a second, unjustified rule against a metric that is now
# performing well.

CONFIRMATION_BARS = 2


def _closes_confirm_stop(direction: str, stop_price: float, closes: pd.Series) -> bool:
    """True if the last CONFIRMATION_BARS closes are all beyond stop_price."""
    if stop_price is None or len(closes) < CONFIRMATION_BARS:
        return True  # not enough history to confirm -- fall back to original decision

    recent = closes.iloc[-CONFIRMATION_BARS:]
    if direction == "long":
        return bool((recent <= stop_price).all())
    if direction == "short":
        return bool((recent >= stop_price).all())
    return True  # unknown direction encoding -- don't second-guess the original


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wraps donchian_ai.should_exit, requiring 2 consecutive closes past
    stop_price before confirming a hard_stop exit.

    All other exit reasons (trend_break / time_stop) pass through unchanged
    -- the ledger shows no evidence problem with those paths since none of
    the 30 closed trades exited via them, and hard_stop performance has
    improved substantially since this filter was deployed.
    """
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag or reason != "hard_stop":
        return exit_flag, reason

    try:
        direction = position.get("direction")
        stop_price = position.get("stop_price")
        closes = df["Close"]
    except Exception:
        return exit_flag, reason  # malformed inputs -- trust the original decision

    if _closes_confirm_stop(direction, stop_price, closes):
        return True, "hard_stop"

    return False, "hard_stop_pending_confirmation"
