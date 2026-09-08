# AI-WRITTEN Phase 2+3 2026-08-30 by claude-sonnet-5
# Entry filter: none -- no Phase 2 entry filter exists yet, pass-through only
# Exit filter: none -- zero closed quality trades available, pass-through only
import pandas as pd

from forex.strategy_advanced_bb_master import generate_signals as _orig_generate_signals
from forex.strategy_advanced_bb_master import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Pass-through wrapper. No exit-reason data exists yet for this
    strategy (0 closed quality trades), so we cannot back any exit
    improvement with evidence. This wrapper preserves the original
    should_exit() behavior unchanged and exists only to satisfy the
    Phase 3 override-module contract, ready to be extended once trade
    history accumulates.
    """
    should_close, reason = _orig_should_exit(position, df, calendar_days_held)
    return should_close, reason
