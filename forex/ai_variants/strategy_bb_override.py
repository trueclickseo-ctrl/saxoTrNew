# AI-WRITTEN Phase 2+3 2026-09-05 by claude-sonnet-5
# Entry filter: none - no Phase 2 entry filter exists yet, pass-through only
# Exit filter: none - exit_reason sample sizes too small/dominated by forced flattens to justify a rule change

from typing import Tuple

import pandas as pd

from forex.strategy_bb import generate_signals as _orig_generate_signals
from forex.strategy_bb import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Pass-through to the original strategy's signal generation (no Phase 2 filter yet)."""
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> Tuple[bool, str]:
    """Pass-through to the original should_exit logic.

    Exit-reason breakdown for this strategy shows only 5 trades that closed via
    the strategy's own exit logic (time_stop=3, hard_stop=1, bb_mid_reversion=1);
    the remaining 11 of 16 quality trades closed via an external roster-flatten
    event unrelated to should_exit's decision logic. With n<=3 per organic exit
    reason, there is no statistically defensible pattern to encode as an override
    (e.g. confirmation bars or breakeven trail) without risking overfitting to
    noise. No change is made; the original decision is returned unmodified.
    """
    exit_flag, exit_reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, exit_reason
