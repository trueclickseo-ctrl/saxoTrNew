# AI-WRITTEN 2026-09-03 by claude-sonnet-5
# Rationale: Exotic-currency crosses (TRY, MXN, CZK, DKK, PLN, NOK, HUF, ZAR, SGD) account for a hugely disproportionate share of hard_stop losses and repeated same-symbol whipsaw re-entries (e.g. CHFMXN stopped out 4x in under 2 hours), while majors/commodities drove essentially all the winners. Block new entries on these exotic-quote pairs.

import pandas as pd
import numpy as np
from forex.strategy_donchian import generate_signals as _orig_generate_signals
from forex.strategy_donchian import should_exit, size_position  # re-export unchanged

# Exotic / low-liquidity currency codes that showed a strong pattern of
# repeated hard_stop losses and rapid re-entry churn in the closed trade
# ledger (net -577 EUR across 20 of 30 sampled trades, only 3 winners).
_EXOTIC_CODES = ("TRY", "MXN", "CZK", "DKK", "PLN", "NOK", "HUF", "ZAR", "SGD")


def _is_exotic_pair(symbol: str) -> bool:
    sym = symbol.upper()
    return any(code in sym for code in _EXOTIC_CODES)


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wraps the original Donchian generate_signals, filtering out exotic-currency
    crosses that historically produced clustered hard_stop losses / re-entry churn.
    """
    signals = _orig_generate_signals(market_data, open_symbols=open_symbols, **kwargs)

    filtered = [s for s in signals if not _is_exotic_pair(s.get("symbol", ""))]

    return filtered
