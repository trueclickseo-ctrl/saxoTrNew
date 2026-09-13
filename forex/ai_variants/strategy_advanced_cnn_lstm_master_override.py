# AI-WRITTEN Phase 2+3 2026-08-30 by claude-sonnet-5
# Entry filter: pass-through (no Phase 2 entry filter exists yet; original generate_signals() called unchanged)
# Exit filter: pass-through (zero closed quality trades; no exit_reason data exists to justify a behavioral change)

import pandas as pd

from forex.strategy_advanced_cnn_lstm_master import generate_signals as _orig_generate_signals
from forex.strategy_advanced_cnn_lstm_master import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None,
                      live_prices: dict = None) -> list:
    return _orig_generate_signals(market_data, open_symbols, live_prices)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """
    Wraps the original should_exit(). Zero closed quality trades exist yet
    for this strategy (total_quality_trades == 0, empty by_exit_reason), so
    there is no data-backed pattern to justify any exit-logic change. Per
    Phase 3 rules, this remains a transparent pass-through so that a future
    re-run (once trades accumulate) can slot in real logic without needing
    a fresh scaffold.
    """
    exit_now, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_now, reason
