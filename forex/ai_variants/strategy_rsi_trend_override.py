# AI-WRITTEN Phase 2+3 2026-09-03 by claude-sonnet-5
# Entry filter: none -- pass-through to original regime-gated RSI(2) signals (no Phase 2 filter justified yet)
# Exit filter: none -- should_exit() left functionally unchanged; exit_reason sample too small/uninformative to safely alter

import pandas as pd

from forex.strategy_rsi_trend import generate_signals as _orig_generate_signals
from forex.strategy_rsi_trend import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """Verbatim pass-through -- no Phase 2 entry filter exists yet for rsi_trend.
    The strategy is already regime-gated at the source-code level (Buy only in
    TRENDING_BULLISH, Sell only in TRENDING_BEARISH), so there is no additional
    entry signal to layer on top without more SIM data."""
    return _orig_generate_signals(market_data, open_symbols=open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wrapper around the original should_exit().

    Exit-reason breakdown reviewed (16 quality trades total):
      - rsi_recovery   (n=9, 77.8% win, avg +7.8)  -> the strategy's normal,
        working exit path. No change warranted -- it is already the best
        performing exit reason and altering it risks degrading a working rule.
      - hard_stop      (n=2, 0% win, avg -18.6)    -> sample size is far too
        small (2 trades) to distinguish a real inefficiency (e.g. premature
        stop placement) from noise. Adding a confirmation-bar or breakeven-
        trail rule on n=2 would be pure overfitting.
      - roster_flatten_2026-09-02 (n=5, 0% win, avg -19.3) -> this is a
        portfolio-level forced flatten tied to a specific calendar event
        (a roster/administrative close-out), not a decision made inside
        should_exit(). It is not reproducible logic that this function
        controls, so no should_exit() change can address it.

    Per governance rule 7: with no exit_reason bucket showing both (a) a
    clear, repeatable pattern and (b) a sample size large enough to trust,
    should_exit() is left functionally unchanged. This wrapper exists so the
    Phase 3 module contract is satisfied and so future SIM data can be
    re-evaluated without needing a new module shape.
    """
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, reason
