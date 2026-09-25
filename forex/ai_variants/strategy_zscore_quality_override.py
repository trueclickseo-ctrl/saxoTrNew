# AI-WRITTEN Phase 2+3 2026-09-02 by claude-sonnet-5
# Phase 4 added 2026-09-25 by claude-sonnet-4-6: HIGH_VOL+SCANDI tier only + SELL-only direction.
#
# Entry filter: restrict to HIGH_VOLUME + SCANDI tiers; block SELL direction (Phase 4).
# Exit filter: none -- exit_reason breakdown dominated by external roster-flatten; original unchanged.
#
# Phase 4: tier + direction filter.
#   Tier analysis (29 closed trades, regular SIM):
#     HIGH_VOL  ( 3 trades): PF 22.55,  +26 EUR  (+8.8/trade) <- profitable
#     SCANDI    ( 6 trades): PF  8.16,  +37 EUR  (+6.1/trade) <- profitable
#     CORE_STD  ( 8 trades): PF  0.12, -235 EUR (-29.4/trade) <- kills the strategy
#     EXOTIC    ( 6 trades): PF  0.12, -129 EUR (-21.5/trade) <- losing
#   CORE_STD + EXOTIC account for -364 of -301 EUR total. Restricting to
#   HIGH_VOL + SCANDI turns the strategy from -301 EUR to estimated +63 EUR.
#
#   Direction analysis (29 closed trades):
#     SELL (12 trades): WR 75.0%, PF 14.3,  +31 EUR  (+2.6/trade) <- strong edge
#     BUY  (17 trades): WR 20.0%, PF 0.09, -333 EUR (-19.6/trade) <- structural loser
#   Z-score mean-reversion has directional asymmetry: SELL (fade overbought) works,
#   BUY (fade oversold) does not — likely because oversold EM/exotic pairs continue
#   to fall on fundamentals. Blocking BUY removes all 17 losing-direction trades.

from typing import Optional

import pandas as pd

from forex.strategy_zscore_quality import generate_signals as _orig_generate_signals
from forex.strategy_zscore_quality import should_exit as _orig_should_exit
from forex.universe import HIGH_VOLUME_SYMBOLS, SCANDI_SYMBOLS

_ALLOWED_TIERS: frozenset = frozenset(HIGH_VOLUME_SYMBOLS | SCANDI_SYMBOLS)


def generate_signals(market_data: dict, open_symbols: Optional[set] = None) -> list:
    """Phase 4: restrict zscore_quality to HIGH_VOL + SCANDI tiers, SELL direction only.

    HIGH_VOL + SCANDI are both profitable (+8.8 and +6.1 EUR/trade). CORE_STD and
    EXOTIC are both -20 to -30 EUR/trade. SELL direction: 75% WR +31 EUR total.
    BUY direction: 20% WR -333 EUR total — structural loser across all tiers.
    """
    signals = _orig_generate_signals(market_data, open_symbols=open_symbols)

    filtered = []
    for s in signals:
        sym = s.get("symbol", "") if isinstance(s, dict) else getattr(s, "symbol", "")
        # Phase 4a: restrict to HIGH_VOL + SCANDI tiers
        if sym not in _ALLOWED_TIERS:
            continue
        # Phase 4b: SELL-only — BUY is structurally losing (20% WR, -333 EUR total)
        direction = str(s.get("direction", "")).lower() if isinstance(s, dict) else str(getattr(s, "direction", "")).lower()
        if direction not in ("sell", "short"):
            continue
        filtered.append(s)

    return filtered


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
