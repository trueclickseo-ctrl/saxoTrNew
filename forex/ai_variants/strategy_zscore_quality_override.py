# AI-WRITTEN Phase 2+3 2026-09-02 by claude-sonnet-5
# Entry filter: none -- pass-through to original generate_signals() (no Phase 2 override exists)
# Exit filter: none -- exit_reason breakdown dominated by an external roster-flatten event and a single stop-loss sample; original should_exit() preserved unchanged

from typing import Optional

import pandas as pd

from forex.strategy_zscore_quality import generate_signals as _orig_generate_signals
from forex.strategy_zscore_quality import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: Optional[set] = None) -> list:
    """Pass-through: no Phase 2 entry filter has been derived yet for
    zscore_quality (only 17 closed SIM trades to date -- too few to safely
    carve out an entry-side rule). Delegates straight to the original."""
    return _orig_generate_signals(market_data, open_symbols=open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Pass-through wrapper.

    Exit-reason breakdown at n=17 closed trades:
      - zscore_reverted (n=10): 90% win rate, avg +18.5 -- the intended,
        working exit, now with a larger and even stronger sample than at
        the n=4 checkpoint. No change warranted; this is exactly the
        mean-reversion behaviour the strategy is designed to capture.
      - roster_flatten_2026-09-02 (n=6): 33% win rate, avg -0.8 -- this
        remains an external, one-off portfolio-level flatten event (a
        research roster change), NOT a decision made inside should_exit().
        It still accounts for 6/17 = 35% of the whole sample but is a
        single calendar-dated administrative action; building an
        in-strategy exit rule around it would be pure overfit to one
        event, and it cannot be detected or avoided from within
        should_exit()'s (position, df, calendar_days_held) signature
        anyway -- it's imposed from outside the strategy.
      - STOP-LOSS hit (n=1): -75.6, still only one observation. One sample
        cannot distinguish a genuine bad-stop-placement pattern (e.g.
        stop too tight, needs confirmation bar) from ordinary noise/tail
        risk. Adding a confirmation-bar delay or breakeven trail on a
        single stop-loss observation risks turning a contained loss into
        a larger one on the next trade if the pattern doesn't repeat.

    Per governance rule 7: with no clear, data-backed exit pattern at this
    sample size (the two 'bad' buckets are either external/administrative
    or n=1), the original should_exit() is called and its decision is
    returned unchanged. This preserves the Phase 2/3 wrapper contract so
    the module can be safely re-evaluated once more SIM trades accumulate,
    particularly more stop-loss exits to establish whether n=1 was noise.
    """
    exit_now, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_now, reason
