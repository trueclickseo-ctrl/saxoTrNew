"""
forex/strategy_gap_quality.py
------------------------------
Gap Quality — EXOTIC-tier-filtered A/B twin of strategy_gap.

Identical to strategy_gap in every way EXCEPT it blocks the 83 EXOTIC-tier
pairs before returning signals. Evidence from 266 closed regular-SIM gap trades:

  HIGH_VOL  (23 trades): PF 34.12, +25,298 EUR (+1,100/trade)
  CORE_STD  (47 trades): PF  5.61, +23,172 EUR  (+493/trade)
  SCANDI    (67 trades): PF  0.87,  -6,136 EUR   (-92/trade)
  METALS    (10 trades): PF  0.45,     -89 EUR    (-9/trade)
  EXOTIC   (119 trades): PF  0.44, -28,981 EUR  (-244/trade) <- structural loser

EXOTIC is 45% of gap volume but destroys P&L. Removing it lifts estimated
all-time net from +13,264 to +48,470 EUR.

This strategy runs in regular SIM alongside the unfiltered `gap` strategy so
we can accumulate live forward evidence for the filter. `gap` continues feeding
the AI evolver with all-tier data; `gap_quality` tests the promoted filter.

All execution logic (session detection, sizing, exits, cooldown, breakeven
trail, live price confirmation) is delegated to strategy_gap unchanged.
"""

from forex.strategy_gap import (
    generate_signals as _orig_generate_signals,
    generate_session_signals as _orig_generate_session_signals,
    should_exit,
    size_position,
)
from forex.universe import EXOTIC_SYMBOLS

_EXOTIC: frozenset = frozenset(s.upper() for s in EXOTIC_SYMBOLS)

# Expose constants the runner reads from every strategy module.
try:
    from forex.strategy_gap import (
        RISK_PCT, MIN_BARS, NEEDS_LIVE_PRICES,
        TIME_STOP_DAYS, GAP_MIN_PCT,
    )
except ImportError:
    pass


def _filter_exotic(signals: list) -> list:
    return [s for s in signals if s.get("symbol", "").upper() not in _EXOTIC]


def generate_signals(market_data: dict, open_symbols: set = None,
                     live_prices: dict = None,
                     exhausted_symbols: set = None, **kwargs) -> list:
    return _filter_exotic(_orig_generate_signals(
        market_data,
        open_symbols=open_symbols,
        live_prices=live_prices,
        exhausted_symbols=exhausted_symbols,
        **kwargs,
    ))


def generate_session_signals(session: str,
                              market_data_h1: dict,
                              open_symbols: set = None,
                              live_prices: dict = None,
                              exhausted_symbols: set = None) -> list:
    return _filter_exotic(_orig_generate_session_signals(
        session,
        market_data_h1,
        open_symbols=open_symbols,
        live_prices=live_prices,
        exhausted_symbols=exhausted_symbols,
    ))
