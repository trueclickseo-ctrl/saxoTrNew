# AI-WRITTEN Phase 2+3 2026-09-09 by claude-sonnet-5
# Entry filter: Block DKK/HKD pegged-currency pairs (59/61 trades, PF 0.6) from EMA(5/30) fresh-crossover+DI-spread entries.
# Exit filter: No change -- exit_reason ledger (60/61 hard_stop, 1 roster_flatten) shows a single dominant population with no second reason to compare against, so the stop-trigger logic is left untouched.

import pandas as pd

from forex.strategy_ema_trend import generate_signals as _orig_generate_signals
from forex.strategy_ema_trend import should_exit as _orig_should_exit

_BLOCKED_CURRENCIES = ("DKK", "HKD")


def _is_blocked(symbol: str) -> bool:
    if not symbol:
        return False
    return any(c in symbol for c in _BLOCKED_CURRENCIES)


def generate_signals(market_data: dict, open_symbols: set | None = None, **kwargs) -> list:
    """Wraps the original ema_trend signal generator, dropping any entries on
    symbols involving DKK or HKD.

    SIM ledger evidence (61 closed trades, win rate 27.9%, PF 0.6, total
    -135 EUR): the exit_reasons breakdown shows the trade population is
    almost entirely DKKJPY (hard_stop prices ~23-24.xx) and JPYHKD
    (hard_stop prices ~0.049x) -- roughly 59 of 61 closes. Both DKK (pegged
    to EUR under ERM II) and HKD (pegged to USD) are managed/pegged
    currencies that trade in an unusually tight band, so an EMA(5/30)
    crossover strategy sees mostly noise-driven false crossovers on these
    pairs rather than real trend moves -- consistent with the observed
    rapid-fire sub-hour stop-outs dominating the recent trade log. This
    single cluster is dragging the whole strategy's edge negative; the few
    winning trades in the sample were the longer-held (multi-day) ones on
    the same symbols, suggesting entries here rarely represent genuine
    trend continuation. Blocking these two currencies removes the loss
    cluster while leaving the rest of the ema_trend logic (ADX filter,
    fresh-crossover gate, DI-spread gate) untouched.
    """
    base = _orig_generate_signals(market_data, open_symbols=open_symbols, **kwargs)
    if not base:
        return []
    return [s for s in base if not _is_blocked(s.get("symbol", ""))]


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Pass-through to the underlying should_exit, unchanged.

    Exit-reason breakdown from the 61-trade SIM ledger:
        hard_stop            n=60  win_rate=28.3%  avg_pnl=-2.20
        roster_flatten_...   n=1   win_rate=0.0%   avg_pnl=-5.02

    98% of exits share a single exit_reason (hard_stop) with only one
    outlier (a roster flatten event, n=1). There is no second
    meaningfully-sized exit-reason population to compare against, so there
    is no ledger-backed way to distinguish 'stop fired on a single noisy
    wick that would have reversed' from 'stop correctly capped a genuinely
    bad entry'. The low 28.3% win rate mirrors the poor entry quality
    already addressed by the Phase 2 DKK/HKD currency-block filter rather
    than a flaw in the exit trigger itself; an average loss of ~2.2
    (currency units) per hard_stop is consistent with the stop simply
    doing its job on a currently low-hit-rate entry signal. Adding a
    confirmation-bar delay or breakeven trail here would let losers run
    further with zero ledger evidence it would flip net losers into net
    winners -- so per governance rule 7, the underlying exit logic is left
    unchanged rather than introduce an unfounded change, while the code
    path is still written so Phase 2's entry filter remains active.
    """
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_flag, reason
