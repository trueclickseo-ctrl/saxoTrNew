# AI-WRITTEN Phase 2+3 2025-06-17 by claude-sonnet-5
# Entry filter: pass-through (no Phase 2 entry filter exists yet for cnn_lstm)
# Exit filter: require 2-consecutive-bar confirmation before honoring a 'model_flip buy' exit trigger (buy-side flips were 0/4 winners in sim data)

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

    Data (20 closed quality trades) shows a sharp split in model_flip exit
    quality by flip direction:
      - 'model_flip buy ...' exits (closing a SHORT because the model flipped
        to Buy): 4 trades, 0 wins, 4 losses, total_pnl = -32.89. Every single
        buy-side flip exit observed so far was a loser.
      - 'model_flip sell ...' exits (closing a LONG because the model flipped
        to Sell): 16 trades, 9 wins, 7 losses, total_pnl strongly positive
        (multiple +20 to +32 winners), i.e. broadly reliable.

    This asymmetry suggests the buy-flip signal is noisier / more prone to
    single-bar false positives near the confidence threshold than the
    sell-flip signal. Rather than blocking buy-flip exits outright (risky on
    n=4), we require 2 consecutive bars/calls where the original logic wants
    to exit on a buy-flip before actually acting on it. Sell-flip, hard_stop,
    and time_stop exits are left completely unchanged since the data does not
    show a problem with them.

    A small per-position counter is stashed in the position dict
    ('_flip_confirm_count') to track consecutive buy-flip exit requests
    across calls; it is cleared whenever the original logic stops requesting
    an exit or once the exit is finally confirmed.
    """
    exit_now, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_now:
        # Original logic doesn't want to exit -- reset any pending confirmation state.
        position.pop('_flip_confirm_count', None)
        return exit_now, reason

    is_buy_flip = isinstance(reason, str) and reason.startswith('model_flip buy')

    if not is_buy_flip:
        # sell-flip / hard_stop / time_stop -- data supports acting immediately.
        position.pop('_flip_confirm_count', None)
        return exit_now, reason

    # Buy-flip exit requested: require a second consecutive confirmation
    # before honoring it, since all 4 observed buy-flip exits were losers.
    count = position.get('_flip_confirm_count', 0) + 1
    position['_flip_confirm_count'] = count

    if count < 2:
        return False, f"exit_deferred_awaiting_confirmation ({reason})"

    position.pop('_flip_confirm_count', None)
    return True, reason
