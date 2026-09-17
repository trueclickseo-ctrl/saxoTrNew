# AI-WRITTEN Phase 2+3 2026-09-18 by claude-sonnet-5
# Entry filter: Exclude TRY, XAU, and HKD cross-pair symbols (consistent losers in closed-trade sample).
# Exit filter: Require 2 consecutive daily closes beyond the ATR hard-stop before confirming a "hard_stop" exit, to filter single-bar wick-outs.

import pandas as pd
import numpy as np
from forex.strategy_ml import generate_signals as _orig_generate_signals
from forex.strategy_ml import should_exit as _orig_should_exit

# Symbol substrings associated with outsized/consistent losses in the closed-trade sample:
#   TRY (Turkish Lira) trades: net -98.81 over 5 trades (prior pass)
#   XAU (gold) cross trades:   net -245.79 over 2 trades (prior pass)
#   HKD (Hong Kong Dollar, USD-pegged) trades: CADHKD -5.02, CADHKD -2.43,
#       GBPHKD -66.96, EURHKD -5.53 -> net -79.94 over 4 trades, 0% WR (this pass)
_EXCLUDE_SUBSTRINGS = ("TRY", "XAU", "HKD")


def _is_excluded(symbol: str) -> bool:
    if not symbol:
        return False
    s = symbol.upper()
    return any(sub in s for sub in _EXCLUDE_SUBSTRINGS)


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrapper around strategy_ml.generate_signals that filters out
    TRY (Turkish Lira), XAU (gold), and HKD (USD-pegged Hong Kong Dollar)
    cross-pair symbols, which showed a consistent pattern of losing trades
    in the closed SIM trade record (37-trade sample as of 2026-09-18).
    """
    if market_data:
        filtered_market_data = {
            sym: df for sym, df in market_data.items() if not _is_excluded(sym)
        }
    else:
        filtered_market_data = market_data

    return _orig_generate_signals(
        market_data=filtered_market_data,
        open_symbols=open_symbols,
        **kwargs,
    )


# --- Phase 3: exit logic override -------------------------------------------------
#
# Closed-trade exit_reason breakdown (37 trades, 2026-09-18 pull):
#   hard_stop:              n=17, WR=35.3%, total_pnl=-494.73, avg=-29.1  <- worst bucket
#   STOP-LOSS hit @ ...:    n=4,  WR=0.0%,  total_pnl=-142.94             <- broker-side fills,
#                                                                             not reachable from should_exit()
#   ml_flip:                n=3,  WR=66.7%, total_pnl=+1.95               <- fine, untouched
#   roster_flatten_...:     n=13, WR=38.5%, total_pnl=+1146.13            <- forced external exit, untouched
#
# The "hard_stop" bucket is both the largest sample and the most consistently
# unprofitable exit path the strategy's own should_exit() controls. A plausible
# cause is single-bar noise/wick touches of the 2.0xATR stop that reverse the
# next day. We add a confirmation requirement: only honor a "hard_stop" exit
# signal if the two most recent daily closes are BOTH beyond the stop price
# (i.e. the breach persists for at least one extra bar). If only the latest
# close breaches the stop, we hold the position one more day rather than exit
# immediately. All other exit reasons (time stop, ml_flip, profit target, etc.)
# pass through unchanged.

_HARD_STOP_MARKERS = ("hard_stop",)


def _is_hard_stop_reason(reason: str) -> bool:
    if not reason:
        return False
    r = reason.lower()
    return any(marker in r for marker in _HARD_STOP_MARKERS)


def _closes_confirm_stop(position: dict, df: pd.DataFrame) -> bool:
    """Return True if the two most recent closes both breach the stop price
    in the direction that would trigger a stop-out (i.e. the breach is
    confirmed, not a single-bar wick).
    """
    try:
        if df is None or len(df) < 2 or "Close" not in df.columns:
            return True  # insufficient data -> defer to original decision

        stop_price = position.get("stop_price")
        direction = position.get("direction")
        if stop_price is None or direction is None:
            return True

        last_close = df["Close"].iloc[-1]
        prev_close = df["Close"].iloc[-2]

        direction_str = str(direction).lower()
        is_long = direction_str in ("long", "buy", "1") or direction_str.startswith("l")

        if is_long:
            return (last_close < stop_price) and (prev_close < stop_price)
        else:
            return (last_close > stop_price) and (prev_close > stop_price)
    except Exception:
        # Any unexpected data issue -> do not block the original exit decision.
        return True


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag:
        return exit_flag, reason

    if _is_hard_stop_reason(reason):
        if not _closes_confirm_stop(position, df):
            return False, "hard_stop_awaiting_confirmation"

    return exit_flag, reason
