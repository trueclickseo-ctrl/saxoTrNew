# AI-WRITTEN Phase 2+3 2026-09-02 by claude-sonnet-5
# Entry filter: none (Phase 2 pass-through -- no entry override exists yet for ema_trend)
# Exit filter: no override -- hard_stop is 56/57 exits with a single homogeneous exit_reason, no differentiating pattern to safely act on

import pandas as pd

from forex.strategy_ema_trend import generate_signals as _orig_generate_signals
from forex.strategy_ema_trend import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """Pass-through: no Phase 2 entry filter exists yet for ema_trend."""
    return _orig_generate_signals(market_data, open_symbols=open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wraps the original should_exit. No discretionary exit-reason pattern
    was found in the ledger data to safely act on (see exit_rationale), so
    the original decision is passed through unchanged. This wrapper exists
    to preserve the Phase 3 module contract and as a hook point for future
    iterations once exit_reason diversity increases."""
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, reason
