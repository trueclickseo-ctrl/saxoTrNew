# AI-WRITTEN Phase 2+3 2026-09-01 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals() (no Phase 2 override existed)
# Exit filter: require 2 consecutive daily closes beyond stop_price before honoring a hard_stop exit, to filter single-bar noise stop-outs

from typing import Tuple

import pandas as pd

from forex.strategy_advanced_rsi_master import generate_signals as _orig_generate_signals
from forex.strategy_advanced_rsi_master import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Pass-through: no entry filter changes yet (insufficient trade sample)."""
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> Tuple[bool, str]:
    """Wrap the original should_exit with a hard_stop confirmation filter.

    All 4 quality closed trades for this strategy so far exited via
    hard_stop with a 0% win rate (avg -$105.80/trade). With only 4 trades
    we cannot conclusively separate 'genuinely adverse move' stops from
    'single-bar noise piercing the stop then closing back inside it', so
    we add a conservative confirmation requirement: a hard_stop signal is
    only honored if the stop breach persists for two consecutive daily
    closes. A one-bar breach that closes back on the correct side of the
    stop is treated as noise and the position is held one more day
    (subject to the original logic re-evaluating on the next bar).
    """
    exit_flag, exit_reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag or exit_reason != "hard_stop":
        return exit_flag, exit_reason

    try:
        direction = position.get("direction")
        stop_price = position.get("stop_price")
        if direction is None or stop_price is None or len(df) < 2:
            return exit_flag, exit_reason

        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])

        if direction == "long":
            breached_last = last_close < stop_price
            breached_prev = prev_close < stop_price
        elif direction == "short":
            breached_last = last_close > stop_price
            breached_prev = prev_close > stop_price
        else:
            return exit_flag, exit_reason

        if breached_last and breached_prev:
            return True, "hard_stop_confirmed"

        # Single-bar breach only -- hold one more day pending confirmation.
        return False, "hard_stop_pending_confirmation"
    except Exception:
        # Any unexpected data issue: fall back to the original decision.
        return exit_flag, exit_reason
