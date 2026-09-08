# AI-WRITTEN 2026-09-03 by claude-sonnet-5
# Rationale: TRY (Turkish Lira) and XAU (gold) cross pairs in the ml strategy's closed trades were dominated by hard-stop / stop-loss exits and produced a combined -344.6 EUR versus the strategy's overall +623 EUR total, while non-TRY/XAU trades (esp. NOK crosses) were strongly profitable.

import pandas as pd
import numpy as np
from forex.strategy_ml import generate_signals as _orig_generate_signals

# Symbol substrings associated with outsized losses in the closed-trade sample:
#   TRY (Turkish Lira) trades: CHFTRY -60.77, AUDTRY -30.12, GBPTRY -12.42,
#       USDTRY -22.11, NZDTRY +26.61  -> net -98.81 over 5 trades
#   XAU (gold) cross trades:   XAUTRY -137.96, XAUTHB -107.83 -> net -245.79
# Combined these 7 trades cost the strategy ~-344.6 EUR out of a +623 EUR total,
# i.e. removing them would have roughly 1.5x'd total P&L on this sample.
_EXCLUDE_SUBSTRINGS = ("TRY", "XAU")


def _is_excluded(symbol: str) -> bool:
    if not symbol:
        return False
    s = symbol.upper()
    return any(sub in s for sub in _EXCLUDE_SUBSTRINGS)


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrapper around strategy_ml.generate_signals that filters out
    TRY (Turkish Lira) and XAU (gold) cross-pair symbols, which showed
    a consistent pattern of large stop-loss losses in the closed SIM
    trade record (34 trades sample).
    """
    if market_data:
        filtered_market_data = {
            sym: df for sym, df in market_data.items() if not _is_excluded(sym)
        }
    else:
        filtered_market_data = market_data

    signals = _orig_generate_signals(filtered_market_data, open_symbols=open_symbols, **kwargs)

    if not signals:
        return signals

    # Defensive post-filter in case original signal objects carry symbol
    # info differently (dict-like or attribute-like).
    def _sig_symbol(sig):
        if isinstance(sig, dict):
            return sig.get("symbol") or sig.get("Symbol")
        return getattr(sig, "symbol", None)

    return [sig for sig in signals if not _is_excluded(_sig_symbol(sig) or "")]
