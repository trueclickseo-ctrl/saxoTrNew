# AI-WRITTEN Phase 2+3 2026-09-10 by claude-sonnet-5
# Entry filter: exclude TRY (Turkish Lira) and XAU (gold) cross-pair symbols, which showed a consistent pattern of large stop-loss losses.
# Exit filter: require 3 consecutive closes beyond the ATR hard-stop level (escalated from 2) before honoring a 'hard_stop' exit -- the 2-bar rule alone still left hard_stop as a net-loss bucket.

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
# Updated closed-trade exit_reason breakdown (36 quality trades) shows:
#   hard_stop            : n=16, win_rate=37.5%, total_pnl=-326.30, avg=-20.4
#   STOP-LOSS hit @ ...   : n=4 individual broker-side fills, all losers
#                           (small n each, these are broker-executed stop
#                           fills that happen outside should_exit() and
#                           cannot be intercepted by this function)
#   ml_flip               : n=3,  win_rate=66.7%, total_pnl=+1.95
#   roster_flatten_...    : n=13, win_rate=38.5%, total_pnl=+1146.13 (forced)
#
# A prior Phase 3 pass already added a 2-consecutive-close confirmation
# rule for the 'hard_stop' reason (the strategy's own ATR-based stop check,
# distinct from broker-side STOP-LOSS fills). That rule modestly improved
# the hard_stop win rate (was 28.6% -> now 37.5%) and average loss (was
# -27.3 -> now -20.4), but the bucket is STILL the single largest net-loss
# exit reason under our control (-326.30 total). This indicates 2-bar
# confirmation still lets some whipsaw stop-outs through. We escalate the
# confirmation requirement from 2 to 3 consecutive closes beyond the stop
# level in the adverse direction before honoring the exit -- if only 1 or 2
# bars have breached so far, we hold and let should_exit() re-evaluate on
# the next bar. Broker-side 'STOP-LOSS hit @ X' fills are outside this
# function's control and are left unchanged.


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag or reason != "hard_stop":
        return exit_flag, reason

    try:
        stop_price = position.get("stop_price")
        direction = (position.get("direction") or "").lower()
        closes = df["Close"]

        if stop_price is None or direction not in ("long", "short") or len(closes) < 3:
            return exit_flag, reason

        last_close = closes.iloc[-1]
        prev_close = closes.iloc[-2]
        prev2_close = closes.iloc[-3]

        if direction == "long":
            breached_last = last_close <= stop_price
            breached_prev = prev_close <= stop_price
            breached_prev2 = prev2_close <= stop_price
        else:
            breached_last = last_close >= stop_price
            breached_prev = prev_close >= stop_price
            breached_prev2 = prev2_close >= stop_price

        if breached_last and not (breached_prev and breached_prev2):
            # Fewer than 3 consecutive confirmed breaches -- hold and
            # let should_exit() re-check on the next bar.
            return False, "hard_stop_pending_confirmation"
    except Exception:
        # Any unexpected data shape: fall back to original decision, don't
        # risk silently overriding a legitimate stop.
        return exit_flag, reason

    return exit_flag, reason
