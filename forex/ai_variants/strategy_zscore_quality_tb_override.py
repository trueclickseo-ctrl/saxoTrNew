# AI-WRITTEN Phase 2+3 2026-09-04 by claude-sonnet-5
# Entry filter: none -- no Phase 2 entry filter exists yet, pass-through to original generate_signals()
# Exit filter: none -- zero closed quality trades available, cannot back a rule with data; pass-through to original should_exit()

import pandas as pd

from forex.strategy_zscore_quality_tb import generate_signals as _orig_generate_signals
from forex.strategy_zscore_quality_tb import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Pass-through to the original strategy's generate_signals().
    No Phase 2 entry filter exists yet for this strategy -- preserved verbatim
    as a no-op wrapper so future phases can extend it in place."""
    return _orig_generate_signals(market_data, open_symbols=open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Pass-through to the original strategy's should_exit().
    No closed quality trades exist yet in pnl_ledger.db for this strategy, so
    there is no exit_reason data to justify an override. Calling the original
    decision unchanged preserves behavior until real trade data accumulates."""
    exit_flag, exit_reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, exit_reason
