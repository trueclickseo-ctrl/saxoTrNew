# AI-WRITTEN Phase 2+3 2026-09-04 by claude-sonnet-5
# Entry filter: exclude TRY (Turkish Lira) and XAU (gold) cross-pair symbols, which showed a consistent pattern of large stop-loss losses.
# Exit filter: require 2 consecutive closes beyond the ATR hard-stop level before honoring a 'hard_stop' exit, to filter single-bar whipsaw stop-outs.

import pandas as pd
import numpy as np
from forex.strategy_ml import generate_signals as _orig_generate_signals
from forex.strategy_ml import should_exit as _orig_should_exit

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


# --- Phase 3: exit logic override -----------------------------------------
#
# Closed-trade exit_reason breakdown (34 quality trades) showed:
#   hard_stop           : n=14, win_rate=28.6%, total_pnl=-382.16, avg=-27.3
#   ml_flip             : n=3,  win_rate=66.7%, total_pnl=+1.95
#   roster_flatten_...  : n=13, win_rate=38.5%, total_pnl=+1146.13 (forced, not ours to change)
#   individual STOP-LOSS hit @ X (broker fills): all losers, small n each
#
# The strategy's own 'hard_stop' exit path (should_exit()'s ATR-based check,
# distinct from broker-side stop fills) is by far the largest and most
# reliably unprofitable exit_reason bucket. A plausible cause is single-bar
# noise briefly poking through the ATR stop level and triggering an exit
# that a very next bar would have reversed. We add a lightweight
# confirmation-bar rule: only honor a 'hard_stop' exit if BOTH the most
# recent close and the prior close have breached the stop level in the
# position's adverse direction. If only the latest close breached it, we
# hold one more bar (return False) and let the original logic re-evaluate
# next time should_exit() is called.


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag or reason != "hard_stop":
        return exit_flag, reason

    try:
        stop_price = position.get("stop_price")
        direction = (position.get("direction") or "").lower()
        closes = df["Close"]

        if stop_price is None or direction not in ("long", "short") or len(closes) < 2:
            return exit_flag, reason

        last_close = closes.iloc[-1]
        prev_close = closes.iloc[-2]

        if direction == "long":
            breached_last = last_close <= stop_price
            breached_prev = prev_close <= stop_price
        else:
            breached_last = last_close >= stop_price
            breached_prev = prev_close >= stop_price

        if breached_last and not breached_prev:
            # Only one bar of confirmation so far -- hold and re-check next bar.
            return False, "hard_stop_pending_confirmation"
    except Exception:
        # Any unexpected data shape: fall back to original decision, don't
        # risk silently overriding a legitimate stop.
        return exit_flag, reason

    return exit_flag, reason
