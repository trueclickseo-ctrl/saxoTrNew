# AI-WRITTEN Phase 2+3 2026-08-29 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals() (no Phase 2 data yet)
# Exit filter: none -- pass-through to original should_exit() (zero closed trades, no pattern to act on)

import pandas as pd

from forex.strategy_donchian_quality import generate_signals as _orig_generate_signals
from forex.strategy_donchian_quality import should_exit as _orig_should_exit


def generate_signals(*args, **kwargs):
    return _orig_generate_signals(*args, **kwargs)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """
    Phase 3 exit wrapper for donchian_quality.

    There are zero closed SIM trades for this strategy yet, so there is no
    exit_reason data to justify any behavioral change. Per policy, we do not
    fabricate a rule without evidence -- this wrapper calls the original
    should_exit() unchanged and simply passes its decision through. This
    file exists so future Phase 3 runs (once trades accumulate) can add
    a data-backed exit rule here without needing a new module registration.
    """
    exit_flag, exit_reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, exit_reason
