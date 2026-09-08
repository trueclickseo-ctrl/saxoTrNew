# AI-WRITTEN Phase 2+3 2026-09-02 by claude-sonnet-5
# Entry filter: none -- pass-through to original bb_quality.generate_signals() (DI-spread gate already built in)
# Exit filter: no change -- sample too small (n=5) and dominant loss bucket (roster_flatten) is a forced system-level close, not an organic exit-logic signal

import pandas as pd

from forex.strategy_bb_quality import generate_signals as _orig_generate_signals
from forex.strategy_bb_quality import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set | None = None) -> list:
    """Verbatim pass-through -- no Phase 2 entry filter exists for bb_quality yet.
    The strategy's own DI-spread non-directional gate is already applied inside
    the original generate_signals()."""
    return _orig_generate_signals(market_data, open_symbols=open_symbols)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wraps the original should_exit(). With only 5 closed quality trades total
    (3 roster_flatten forced closures, 1 hard_stop, 1 bb_mid_reversion win), there
    is no statistically meaningful exit_reason pattern to encode:
      - roster_flatten_2026-09-02 (3 losses) is a one-off system/roster event, not
        a recurring organic exit trigger -- nothing in should_exit() caused it, so
        there is no rule to add against it.
      - hard_stop has n=1 -- a single large loss is not evidence of a fixable
        stop-placement or confirmation-bar defect.
      - bb_mid_reversion has n=1 and was a win.
    Per governance, we preserve the original decision unchanged rather than
    curve-fit an exit rule to 1-3 trades, while still keeping this module as the
    designated Phase 3 hook point for future re-evaluation once more quality
    trades accumulate."""
    should_close, reason = _orig_should_exit(position, df, calendar_days_held)
    return should_close, reason
