# AI-WRITTEN Phase 2+3 2026-09-02 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals() (no Phase 2 override exists)
# Exit filter: none -- exit_reason sample too small/contaminated to support a rule change; original should_exit() preserved unchanged

from typing import Optional

import pandas as pd

from forex.strategy_zscore_quality import generate_signals as _orig_generate_signals
from forex.strategy_zscore_quality import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: Optional[set] = None) -> list:
    """Pass-through: no Phase 2 entry filter has been derived yet for
    zscore_quality (only 11 closed SIM trades to date -- too few to safely
    carve out an entry-side rule). Delegates straight to the original."""
    return _orig_generate_signals(market_data, open_symbols=open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Pass-through wrapper.

    Exit-reason breakdown at n=11 closed trades:
      - zscore_reverted (n=4): 75% win rate, avg +24.4 -- the intended,
        working exit. No change warranted.
      - roster_flatten_2026-09-02 (n=6): 33% win rate, avg -0.8 -- this is
        an external, one-off portfolio-level flatten event (a research
        roster change), NOT a decision made inside should_exit(). It cannot
        be fixed by editing exit logic here, and building a rule around a
        single calendar-dated administrative event would be pure overfit.
      - STOP-LOSS hit (n=1): -75.6, but n=1 gives no statistical basis for
        a confirmation-bar or breakeven-trail rule -- one sample cannot
        distinguish a bad stop-placement pattern from noise.

    Per governance rule 7: with no clear, data-backed exit pattern at this
    sample size, the original should_exit() is called and its decision is
    returned unchanged, preserving the Phase 2/3 wrapper contract so this
    module can be safely re-evaluated once more SIM trades accumulate.
    """
    exit_now, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_now, reason
