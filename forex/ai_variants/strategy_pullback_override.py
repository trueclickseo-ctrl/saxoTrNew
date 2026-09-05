# AI-WRITTEN 2026-09-03 by claude-sonnet-5
# Rationale: NZD-cross trades account for ~71% of total realized losses (-782 of -1101 EUR) across 14 of 35 closed trades, including two outsized hard-stop losses (-213, -210) on the same 2026-08-22 roster entry. Filtering out NZD-involved symbols removes the worst-performing cluster.

import pandas as pd
import numpy as np
from forex import strategy_pullback as _orig


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap original pullback generate_signals, filtering out NZD-involved pairs.

    Trade history shows NZD-quoted/based crosses (NZDHKD, NZDUSD, NZDSGD,
    NZDPLN, NZDCNH) produced the vast majority of realized losses for this
    strategy (~71% of total -1101 EUR pnl from just 14/35 trades), including
    two catastrophic hard-stop losses on the same roster-entry timestamp.
    This filter removes any signal whose symbol contains 'NZD'.
    """
    signals = _orig.generate_signals(market_data, open_symbols=open_symbols, **kwargs)

    filtered = []
    for sig in signals:
        sym = sig.get("symbol", "") if isinstance(sig, dict) else getattr(sig, "symbol", "")
        if "NZD" in sym.upper():
            continue
        filtered.append(sig)

    return filtered
