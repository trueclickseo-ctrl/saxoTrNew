# AI-WRITTEN Phase 2+3 2026-09-05 by claude-sonnet-5
# Entry filter: none (no Phase 2 override existed; pass-through to original generate_signals)
# Exit filter: require two consecutive supertrend_reversal exit signals (confirmation bar) before honoring the reversal exit, since single-bar reversal exits were 0% win rate over 19 trades

import pandas as pd

from forex.strategy_supertrend import generate_signals as _orig_generate_signals
from forex.strategy_supertrend import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """
    Wraps the original should_exit(). The original supertrend_reversal exit
    fired on 19/19 losing quality trades (avg -14.4 pips/unit, 0% win rate),
    strongly suggesting single-bar whipsaw crossbacks through the SuperTrend
    band that reverse again before doing real damage. We now require the
    reversal condition to persist for two consecutive evaluations (bars)
    before allowing the exit. Hard stop and time stop exits are passed
    through unchanged since they showed no such pattern (only 1 and 3
    trades respectively, both from unrelated causes like roster flattening).
    """
    exit_now, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_now:
        # Condition not met this bar -- clear any pending confirmation flag.
        position['_st_reversal_pending'] = False
        return exit_now, reason

    reason_lower = (reason or "").lower()
    if "revers" in reason_lower:
        if position.get('_st_reversal_pending'):
            # Second consecutive bar confirming reversal -- allow the exit.
            position['_st_reversal_pending'] = False
            return True, reason
        else:
            # First bar triggering reversal -- wait for confirmation.
            position['_st_reversal_pending'] = True
            return False, ""
    else:
        # Non-reversal exit (hard stop, time stop, etc.) -- pass through.
        position['_st_reversal_pending'] = False
        return exit_now, reason
