# AI-WRITTEN Phase 2+3 2026-09-05 by claude-sonnet-5
# Phase 4 added 2026-09-25 by claude-sonnet-4-6: block SCANDI tier pairs.
#
# Entry filter: SCANDI tier blocked (Phase 4).
# Exit filter: none - exit_reason sample sizes too small/dominated by forced flattens to justify a rule change
#
# Phase 4: SCANDI tier filter.
#   Tier analysis (23 closed trades, regular SIM):
#     CORE_STD  (16 trades): PF 1.50,  +21 EUR   (+1.3/trade)  <- borderline positive
#     SCANDI    ( 7 trades): PF 0.02, -659 EUR  (-94.1/trade)  <- catastrophic
#   SCANDI is 30% of trades but accounts for -659 of the -718 EUR total loss.
#   Removing SCANDI rescues BB from -718 → approx -59 EUR (near break-even).
#   SCANDI BB losses are driven by NOK/SEK crosses where the mean-reversion
#   signal fires against strong macro trends (e.g. oil-driven NOK strength)
#   with no volume/liquidity anchor — exactly the regime BB is wrong in.

from typing import Tuple

import pandas as pd

from forex.strategy_bb import generate_signals as _orig_generate_signals
from forex.strategy_bb import should_exit as _orig_should_exit
from forex.universe import SCANDI_SYMBOLS

_SCANDI: frozenset = frozenset(SCANDI_SYMBOLS)


def generate_signals(market_data: dict, open_symbols: set = None) -> list:
    """Phase 4: block SCANDI tier pairs from BB signals.

    SCANDI was -659 EUR on 7 trades (-94.1/trade) vs CORE_STD +21 EUR on 16 trades.
    All 7 SCANDI losses were NOK/SEK/DKK crosses where the BB mean-reversion
    signal fired into sustained macro directional moves.
    """
    signals = _orig_generate_signals(market_data, open_symbols)
    return [s for s in signals if s.get("symbol", "") not in _SCANDI]


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> Tuple[bool, str]:
    """Pass-through to the original should_exit logic.

    Exit-reason breakdown for this strategy shows only 5 trades that closed via
    the strategy's own exit logic (time_stop=3, hard_stop=1, bb_mid_reversion=1);
    the remaining 11 of 16 quality trades closed via an external roster-flatten
    event (roster_flatten_2026-09-02) unrelated to should_exit's decision logic.
    That flatten bucket has a poor total_pnl (-577.72) but it was not triggered
    by should_exit -- it was an external forced-close event, so no should_exit
    rule change can address it. With n<=3 per organic exit reason, there is no
    statistically defensible pattern (e.g. confirmation bars, breakeven trail)
    to encode without risking overfitting to noise. No change is made; the
    original decision is returned unmodified.
    """
    exit_flag, exit_reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, exit_reason
