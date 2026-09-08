# AI-WRITTEN Phase 2+3 2026-09-05 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals (no Phase 2 filter existed)
# Exit filter: require 2 consecutive daily closes beyond stop_price before honoring a hard_stop exit, to reduce single-bar whipsaw stop-outs

import pandas as pd

from forex.strategy_advanced_pullback_master import generate_signals as _orig_generate_signals
from forex.strategy_advanced_pullback_master import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    orig_exit, orig_reason = _orig_should_exit(position, df, calendar_days_held)

    if not orig_exit:
        return orig_exit, orig_reason

    reason_text = (orig_reason or "").lower()

    # hard_stop dominated the loss side of the SIM record (5 of 7 quality trades,
    # total_pnl -73.84 despite a 60% win rate on that bucket -- large single-bar
    # stop hits are the primary drag). Require confirmation that price has closed
    # beyond the stop on two consecutive bars before honoring the exit, to filter
    # single-bar spikes/noise through the stop level. Any other exit reason
    # (e.g. roster_flatten, time_stop) is passed through unchanged since the data
    # shows no problem there.
    if "stop" in reason_text:
        stop_price = position.get("stop_price")
        direction = position.get("direction")
        closes = df["Close"] if "Close" in df.columns else None

        if stop_price is None or direction is None or closes is None or len(closes) < 2:
            return orig_exit, orig_reason

        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])

        if direction == "Buy":
            breached_twice = last_close < stop_price and prev_close < stop_price
        elif direction == "Sell":
            breached_twice = last_close > stop_price and prev_close > stop_price
        else:
            breached_twice = True

        if breached_twice:
            return True, orig_reason

        # Track that we deferred an exit once; still let the underlying stop
        # logic re-evaluate on the next bar (state kept on the position dict
        # for visibility/debugging, not required for the logic to function).
        position["_hard_stop_confirm_pending"] = position.get("_hard_stop_confirm_pending", 0) + 1
        return False, "hard_stop_awaiting_confirmation"

    return orig_exit, orig_reason
