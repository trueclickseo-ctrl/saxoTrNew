# AI-WRITTEN Phase 2+3 2026-09-03 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals (no Phase 2 override exists)
# Exit filter: unchanged -- insufficient sample (1 closed trade) to justify a rule change

from typing import Optional

import pandas as pd

from forex.strategy_rsi_atr import generate_signals as _orig_generate_signals
from forex.strategy_rsi_atr import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: Optional[set] = None) -> list:
    """Pass-through: no Phase 2 entry filter exists yet for rsi_atr."""
    return _orig_generate_signals(market_data, open_symbols=open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wrapper around the original should_exit().

    With only 1 closed quality trade on record (a single hard_stop loss),
    there is no statistically meaningful pattern to act on -- a single
    data point cannot distinguish a real hard_stop weakness from noise.
    The original exit logic is therefore preserved unchanged. This wrapper
    exists to keep the Phase 2/3 override interface consistent and to be
    ready to receive a data-backed rule once more quality trades close.
    """
    should_close, reason = _orig_should_exit(position, df, calendar_days_held)
    return should_close, reason
