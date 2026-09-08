# AI-WRITTEN Phase 2+3 2026-08-29 by claude-sonnet-5
# Entry filter: none -- no Phase 2 entry filter exists yet, pass-through to original generate_signals()
# Exit filter: none -- zero closed quality trades in pnl_ledger.db, no data to justify any exit change

import pandas as pd

from forex.strategy_gap_weekend import generate_signals as _orig_generate_signals
from forex.strategy_gap_weekend import should_exit as _orig_should_exit


def generate_signals(*args, **kwargs):
    """Pass-through wrapper -- no Phase 2 entry filter exists yet for gap_weekend.
    Calls the original strategy's generate_signals() unchanged."""
    return _orig_generate_signals(*args, **kwargs)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Pass-through wrapper -- no closed quality trades exist yet for gap_weekend,
    so there is no exit_reason data to justify any change to the original exit
    logic. This wrapper exists to preserve the Phase 3 contract and can be
    revisited once real trade history accumulates."""
    should_close, reason = _orig_should_exit(position, df, calendar_days_held)
    return should_close, reason
