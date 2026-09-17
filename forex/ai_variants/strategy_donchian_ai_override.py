# AI-WRITTEN Phase 2+3 2025-06-16 by claude-sonnet-5
# Entry filter: Correlated JPY-cross basket (HKDJPY/JPYHKD/USDJPY) group cooldown of 12h to block simultaneous correlated stack-ups.
# Exit filter: Require 2 consecutive daily closes beyond stop_price before confirming hard_stop exit, to filter single-bar wick/whipsaw stop-outs (unchanged -- no new exit-reason pattern in updated ledger).

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


# ── Exit filter (Phase 3) ────────────────────────────────────────────────────────
#
# Evidence (updated ledger, 56 quality trades): ALL 56 (100%) still exit via
# hard_stop, win rate 30.4%, avg_pnl -1.1R -- statistically unchanged from
# the prior 54-trade snapshot (29.6% WR, -1.0R avg) that justified this
# confirmation-bars filter. There is still zero trend_break or time_stop
# representation, meaning no new exit-reason pattern has emerged to target.
# The 2-consecutive-close confirmation already deployed remains the correct,
# data-backed response: it filters single-bar wick/whipsaw stop violations
# without altering genuine directional stop-outs. No further change is
# warranted until a different exit_reason distribution appears in the
# ledger.

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
    the 56 closed trades exited via them.
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
