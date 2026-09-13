# AI-WRITTEN Phase 2+3 2026-09-13 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals()
# Exit filter: none -- pass-through to original should_exit(), sample still dominated by external forced flatten

import pandas as pd

from forex.strategy_zscore import generate_signals as _orig_generate_signals
from forex.strategy_zscore import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    # Exit-reason breakdown (16 quality trades) still shows the same pattern
    # noted in the prior Phase-3 pass: 14 of 16 closed trades were closed by
    # 'roster_flatten_2026-09-02', an external portfolio-level forced
    # liquidation event that fires regardless of should_exit()'s own logic.
    # Only 2 trades actually exercised the strategy's native exit paths
    # (both 'zscore_reverted': 1 win, 1 loss). That is far too small a
    # sample to support any rule change to zscore_reverted / hard_stop /
    # time_stop -- and the poor win-rate/avg-pnl concentrated in
    # roster_flatten is a symptom of the external flatten event, not a flaw
    # in this function's decision logic, so no exit-logic change here would
    # address it. Preserving original exit behavior unchanged pending a
    # larger sample of trades that close under normal (non-flatten)
    # conditions.
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, reason
