# AI-WRITTEN Phase 2+3 2026-09-04 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals (no Phase 2 override exists)
# Exit filter: none -- 0 closed quality trades available, original should_exit preserved unchanged

import pandas as pd

from forex.strategy_bb_quality_hv import generate_signals as _orig_generate_signals
from forex.strategy_bb_quality_hv import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """Pass-through: no Phase 2 entry filter exists yet for this strategy."""
    return _orig_generate_signals(market_data, open_symbols=open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Pass-through wrapper.

    No closed quality trades exist yet in pnl_ledger.db for bb_quality_hv
    (total_quality_trades = 0, by_exit_reason = {}). With zero sample size
    there is no data-backed pattern to justify overriding any exit reason
    (e.g. tightening trend_break, adding confirmation bars, or blocking
    exits in a given regime). Per governance rule, the original exit logic
    is called and its decision is returned unchanged so this module still
    exists as a Phase 3 wrapper and can be revisited once trades accumulate.
    """
    exit_flag, exit_reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, exit_reason
