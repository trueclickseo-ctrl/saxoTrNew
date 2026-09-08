# AI-WRITTEN Phase 2+3 2025-06-17 by claude-sonnet-5
# Entry filter: pass-through (no Phase 2 entry filter exists yet for cnn_lstm)
# Exit filter: pass-through (no closed quality trades yet to derive an exit rule from)

from typing import Tuple

import pandas as pd

from forex.strategy_cnn_lstm import generate_signals as _orig_generate_signals
from forex.strategy_cnn_lstm import should_exit as _orig_should_exit


def generate_signals(*args, **kwargs):
    """Pass-through wrapper -- no Phase 2 entry filter exists yet for cnn_lstm."""
    return _orig_generate_signals(*args, **kwargs)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> Tuple[bool, str]:
    """
    Phase 3 exit wrapper for cnn_lstm.

    No closed quality trades exist yet in pnl_ledger.db for this strategy, so
    there is no exit_reason data to justify a change in exit behavior. To avoid
    introducing an unvalidated exit rule on zero evidence, this wrapper simply
    delegates to the original should_exit() unchanged. Once trades accumulate
    and an exit_reason breakdown is available (e.g. model-flip exits vs ATR-stop
    exits vs time-stop exits with distinct win rates), this module should be
    re-run through Phase 3 to add a data-backed rule (e.g. confirmation-bar
    requirement on model-flip exits, or a breakeven trail once price has moved
    favorably by some ATR multiple).
    """
    exit_now, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_now, reason
