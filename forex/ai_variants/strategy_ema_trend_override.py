# AI-WRITTEN Phase 2+3 2026-09-10 by claude-sonnet-5
# Entry filter: Block DKK/HKD pegged-currency pairs (59/61 closed trades, PF 0.6, total -135 EUR) from EMA(5/30) fresh-crossover+DI-spread entries.
# Exit filter: No change to exit logic -- exit_reason ledger still has only one populated bucket (hard_stop, 60/61 trades, win rate 28.3%) plus a single unrelated roster_flatten row, so there is no differential signal (e.g. trend_break vs time_stop, or a regime split) to justify altering the stop-exit decision; pass-through wrapper retained for future instrumentation.

import pandas as pd

from forex.strategy_ema_trend import generate_signals as _orig_generate_signals
from forex.strategy_ema_trend import should_exit as _orig_should_exit
from forex.strategy_ema_trend import size_position as _orig_size_position
from forex.strategy_ema_trend import trailing_stop_update as _orig_trailing_stop_update

# re-export constants so runner/dashboard introspection keeps working
from forex.strategy_ema_trend import (
    FAST_EMA, SLOW_EMA, ADX_PERIOD, ADX_MIN, ATR_PERIOD, ATR_STOP_MULT,
    RISK_PCT, MAX_POSITIONS, TIME_STOP_DAYS, LOT_ROUND, MIN_BARS,
    MAX_CROSSOVER_AGE, DI_SPREAD_MIN,
)

_BLOCKED_CURRENCIES = ("DKK", "HKD")


def _is_blocked(symbol: str) -> bool:
    if not symbol:
        return False
    return any(c in symbol for c in _BLOCKED_CURRENCIES)


def generate_signals(market_data: dict, open_symbols: set | None = None, **kwargs) -> list:
    """Wraps the ema_trend signal generator, dropping any entries on symbols
    involving DKK or HKD.

    SIM ledger evidence (61 closed trades, win rate 27.9%, PF 0.6, total
    -135 EUR): 59 of 61 closes are on DKKJPY / JPYHKD, almost all exiting via
    rapid-fire (sub-30-minute to few-hour) hard_stop hits. Both DKK (pegged
    to EUR under ERM II) and HKD (pegged to USD) trade in an unusually tight,
    managed band, so EMA(5/30) crossovers on these pairs are dominated by
    noise-driven false signals rather than genuine trend continuation --
    exactly the population dragging this strategy's edge negative despite the
    fresh-crossover + DI-spread conviction gates. Blocking these two
    currencies removes the loss cluster while leaving the rest of the
    ema_trend logic (ADX filter, fresh-crossover gate, DI-spread gate)
    untouched.
    """
    base = _orig_generate_signals(market_data, open_symbols=open_symbols, **kwargs)
    if not base:
        return []
    return [sig for sig in base if not _is_blocked(sig.get("symbol", ""))]


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Pass-through wrapper around the base ema_trend should_exit().

    Exit-reason breakdown for this strategy's 61 quality closed SIM trades
    remains essentially a single bucket: hard_stop (60/61, win rate 28.3%,
    avg -2.2, PF 0.6); the lone remaining trade is a roster_flatten event
    unrelated to strategy logic. With only one exit-reason category
    populated there is no differential evidence (e.g. a distinct
    trend_break-in-ranging cluster, a time_stop cluster with a different
    win rate, or a cluster of stop-outs reversing shortly after) to justify
    tightening, loosening, delaying via confirmation bars, or gating the
    stop-exit decision. hard_stop is this strategy's core risk-management
    exit for a ~28%-win-rate trend system; adding a confirmation-bar delay
    or blanket suppression here would increase risk exposure without any
    ledger evidence that the stop itself is noise-triggered as opposed to
    reflecting the expected base rate of stop-outs. This wrapper therefore
    calls the original unchanged and exists so exit-reason instrumentation
    can be added here later once exit_reason diversity appears in the
    ledger (e.g. once DKK/HKD-driven noise trades are filtered out by the
    Phase 2 entry gate and a cleaner sample of exit reasons accumulates).
    """
    exit_now, reason = _orig_should_exit(position, df, calendar_days_held)
    return exit_now, reason


size_position = _orig_size_position
trailing_stop_update = _orig_trailing_stop_update
