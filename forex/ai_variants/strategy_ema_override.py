# AI-WRITTEN Phase 2+3 2026-09-04 by claude-sonnet-5
# Entry filter: per-symbol 12h cooldown to prevent rapid whipsaw re-entry cascades
# Exit filter: require 2 consecutive should_exit() calls signaling a hard-stop breach before honoring the exit, to filter single-bar wick spikes

import datetime as _dt
from typing import Dict, Tuple

import pandas as pd

from forex.strategy_ema import generate_signals as _orig_generate_signals
from forex.strategy_ema import should_exit as _orig_should_exit

# Minimum hours between successive signal emissions for the same symbol.
COOLDOWN_HOURS = 12.0

# Module-level state: last time we emitted (allowed through) a signal per symbol.
_last_signal_time: Dict[str, _dt.datetime] = {}


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap the original EMA crossover strategy with a per-symbol signal cooldown.

    Trade history showed dozens of hard-stop losses on the same NZD-cross
    symbols fired within 30-60 minutes of each other on the same day -- a
    repeated whipsaw re-entry pattern. This wrapper suppresses new entry
    signals for a symbol until COOLDOWN_HOURS have elapsed since the last
    signal we allowed through for that symbol, regardless of what the
    underlying indicators say.
    """
    raw_signals = _orig_generate_signals(market_data, open_symbols=open_symbols, **kwargs)

    if not raw_signals:
        return raw_signals

    now = _dt.datetime.now()
    filtered = []
    for sig in raw_signals:
        sym = sig.get("symbol")
        last = _last_signal_time.get(sym)
        if last is not None:
            elapsed_hours = (now - last).total_seconds() / 3600.0
            if elapsed_hours < COOLDOWN_HOURS:
                continue
        filtered.append(sig)
        _last_signal_time[sym] = now

    return filtered


# Per-symbol counter of consecutive should_exit() calls that returned a
# hard-stop exit. Exit-reason data showed hard_stop accounted for 49 of 52
# (94%) closed trades with only a 6.1% win rate -- essentially the strategy's
# entire loss profile funnels through this one exit path. FX daily bars
# frequently show a brief wick/spike through a level (session rollovers,
# news prints) that closes back inside range on the next bar. Requiring the
# breach to persist for a second consecutive check before honoring the exit
# filters out single-bar noise without weakening the stop's protection against
# a genuine, sustained adverse move.
_stop_breach_count: Dict[str, int] = {}

CONFIRMATION_CALLS_REQUIRED = 2


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> Tuple[bool, str]:
    """Wrap the original should_exit with a confirmation filter on hard-stop exits.

    Exit-reason data showed hard_stop exits made up 49/52 (94%) of all closed
    trades with only a 6.1% win rate -- by far the dominant loss driver.
    Crossover-reversal and time-stop/roster-flatten exits were rare (3 trades
    total) and are passed through unchanged since they were not implicated in
    the loss cluster. For hard-stop exits specifically, we require the stop
    breach to still be present on the *next* should_exit() call for the same
    symbol before actually exiting, filtering transient wick spikes. If the
    breach persists, the original stop still fires on the very next bar --
    this adds at most one bar of delay and never removes the stop entirely.
    """
    should_exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    sym = position.get("symbol")

    if not should_exit_flag:
        if sym is not None:
            _stop_breach_count[sym] = 0
        return should_exit_flag, reason

    reason_lower = (reason or "").lower()

    # Only intercept the hard-stop path; let crossover reversal / time stop /
    # roster-flatten exits fire immediately -- those were rare and not tied
    # to the loss data.
    if "hard_stop" not in reason_lower or sym is None:
        return should_exit_flag, reason

    count = _stop_breach_count.get(sym, 0) + 1
    _stop_breach_count[sym] = count

    if count >= CONFIRMATION_CALLS_REQUIRED:
        _stop_breach_count[sym] = 0
        return True, reason

    # First breach observed -- hold off one more check to filter a possible
    # single-bar wick spike through the stop level.
    return False, "hard_stop_awaiting_confirmation"
