# AI-WRITTEN Phase 2+3 2026-09-10 by claude-sonnet-5
# Entry filter: none -- no Phase 2 entry override exists yet, pass-through only
# Exit filter: require 2 consecutive daily closes beyond stop_price before honoring a hard-stop exit

import pandas as pd
import numpy as np

from forex.strategy_rsi import generate_signals as _orig_generate_signals
from forex.strategy_rsi import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Pass-through wrapper -- no Phase 2 entry filter yet exists for the
    rsi strategy. Kept as a separate function per the override convention
    so Phase 3 exit logic can be layered on without touching entries."""
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """
    Wrap the original RSI(2) should_exit() with a confirmation-bar rule
    applied only to the hard-stop exit path.

    Updated exit-reason breakdown (152 quality trades) still shows the same
    pattern documented when this rule was first written:
      - rsi_recovery: 69 trades, 69.6% win rate, +1737.28 total pnl (healthy, untouched)
      - hard_stop:    41 trades, only 2.4% win rate, -2302.06 total pnl
      - nine one-off "STOP-LOSS hit @ <price>" rows: 9 trades, 0% win rate, all losses (~-635 combined)
      - roster_flatten_2026-09-02: 32 trades, 31.2% win rate -- an operational
        roster event, not an exit-logic defect, so left untouched here.

    The hard-stop / raw "STOP-LOSS hit" bucket (50 trades combined) losing on
    essentially every occurrence is consistent with single-bar wick or
    whipsaw touches of the stop level that would have reversed by the next
    bar. Since this module cannot re-derive or move the stop (that is the
    runner's job per the strategy docstring), the only lever available here
    is confirmation: require the position's stop_price to be breached on
    the close of TWO consecutive daily bars before honoring the exit. A
    single-bar breach is treated as unconfirmed and the position is held
    one more bar, letting the runner's ratchet continue to manage the stop.
    rsi_recovery and time-stop exits are passed through unchanged since
    they already perform well / are structural (not stop-related).

    This is the same rule introduced in the prior Phase 3 revision. The
    fresh 152-trade sample reproduces the identical failure signature
    (hard_stop still the dominant loser bucket) rather than showing a new
    or different pattern, and most of the 41 hard_stop trades in this
    sample predate the confirmation-bar rule's deployment (2026-09-03), so
    there is not yet an independent out-of-sample read on whether it
    helped. Per the evolution rules, absent a *new* data-backed pattern the
    existing, already-justified rule is preserved rather than replaced
    with an untested variant.
    """
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag:
        return exit_flag, reason

    reason_lower = str(reason).lower()
    is_hard_stop = ("stop" in reason_lower) and ("rsi" not in reason_lower) and ("time" not in reason_lower)

    if not is_hard_stop:
        return exit_flag, reason

    if df is None or len(df) < 2:
        return exit_flag, reason

    stop_price = position.get("stop_price")
    direction  = str(position.get("direction", "")).lower()
    if stop_price is None or not direction:
        return exit_flag, reason

    try:
        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])
    except (IndexError, ValueError, TypeError):
        return exit_flag, reason

    if direction.startswith("b") or direction == "long":
        breached_last = last_close < stop_price
        breached_prev = prev_close < stop_price
    else:
        breached_last = last_close > stop_price
        breached_prev = prev_close > stop_price

    if breached_last and breached_prev:
        return True, reason

    # Only the most recent close breached the stop -- treat as an
    # unconfirmed whipsaw and hold one more bar.
    return False, "hard_stop_awaiting_confirmation"
