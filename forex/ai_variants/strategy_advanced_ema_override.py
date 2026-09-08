# AI-WRITTEN Phase 2+3 2026-09-03 by claude-sonnet-5
# Entry filter: none -- no Phase 2 entry filter exists yet, pass-through to original generate_signals()
# Exit filter: none -- insufficient exit data (1 closed trade, roster_flatten only), pass-through to original should_exit()

import pandas as pd

from forex.strategy_advanced_ema import generate_signals as _orig_generate_signals
from forex.strategy_advanced_ema import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)
    # No override applied: only 1 closed quality trade exists in the sample,
    # and its exit_reason was "roster_flatten" (an administrative end-of-window
    # flatten, not a strategy-generated exit signal such as stop/trend-break/
    # time-stop). There is no evidence in the data of a systematic exit-logic
    # weakness (e.g. no losing trades, no repeated bad exit_reason categories)
    # to justify a rule change. Passing the original decision through unchanged
    # to avoid overfitting an exit rule to a single, non-representative sample.
    return exit_flag, reason
