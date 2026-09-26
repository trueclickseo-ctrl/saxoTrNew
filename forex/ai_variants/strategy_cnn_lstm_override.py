# AI-WRITTEN Phase 2+3 2025-06-17 by claude-sonnet-5
# Entry filter: pass-through (no Phase 2 entry filter exists yet for cnn_lstm)
# Exit filter: require 2-consecutive-bar confirmation before honoring a 'model_flip buy' exit trigger (buy-side flips were 0/4 winners in sim data)
# SUPERSEDED 2026-09-27: 2-bar model_flip buy confirmation promoted to strategy_cnn_lstm.py should_exit().
# This file kept as a no-op passthrough so ai_sim still loads it without error.
#
# PENDING ACTION FOR EVOLVER (do not remove this comment until actioned):
# When closed organic trades >= 50: raise CONFIDENCE_THRESHOLD from 0.45 to 0.65
# in generate_signals() by importing and overriding the constant before calling
# _orig_generate_signals. Evidence: current threshold 0.45 lets weak-signal trades
# through; 23 closed trades show PF=1.30 -- raising threshold should cut low-confidence
# entries and improve PF further. Human decision logged 2026-09-18.

from typing import Tuple

import pandas as pd

from forex.strategy_cnn_lstm import generate_signals as _orig_generate_signals
from forex.strategy_cnn_lstm import should_exit as _orig_should_exit


def generate_signals(*args, **kwargs):
    """Pass-through wrapper -- no Phase 2 entry filter exists yet for cnn_lstm."""
    return _orig_generate_signals(*args, **kwargs)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> Tuple[bool, str]:
    """Pass-through — fix promoted to strategy_cnn_lstm.py (2026-09-27)."""
    return _orig_should_exit(position, df, calendar_days_held)
