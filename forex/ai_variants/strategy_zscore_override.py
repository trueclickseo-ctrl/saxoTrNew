# AI-WRITTEN Phase 2+3 2026-09-06 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals()
# Exit filter: none -- pass-through to original should_exit(), data still dominated by external forced flatten

import pandas as pd

from forex.strategy_zscore import generate_signals as _orig_generate_signals
from forex.strategy_zscore import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    return _orig_generate_signals(market_data, open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    # No reliable exit-logic pattern to act on: 14 of 15 closed trades were
    # closed by an external 'roster_flatten_2026-09-02' event (a portfolio-
    # level forced liquidation unrelated to should_exit()'s own decisions),
    # leaving only 1 trade that actually exercised the strategy's native
    # zscore_reverted / hard_stop / time_stop logic. That single sample
    # (a winner) is not enough to justify any rule change -- the poor
    # win-rate/avg-pnl on 'roster_flatten_2026-09-02' reflects an external
    # forced-liquidation event, not a flaw in should_exit()'s own decision
    # logic (zscore reversion, hard stop, time stop), so no exit-logic fix
    # here would address it. Preserving original exit behavior unchanged
    # pending a larger sample of trades that close under normal
    # (non-flatten) conditions.
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, reason
