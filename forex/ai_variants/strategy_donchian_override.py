# AI-WRITTEN Phase 2+3 2026-09-04 by claude-sonnet-5
# Entry filter: Block new entries on exotic-quote currency pairs (TRY, MXN, CZK, DKK, PLN, NOK, HUF, ZAR, SGD) due to clustered hard_stop losses / re-entry churn.
# Exit filter: Require 2 consecutive daily closes past the ATR hard-stop level before honoring a hard_stop exit, to filter single-bar whipsaw stop-outs.

import pandas as pd
import numpy as np
from forex.strategy_donchian import generate_signals as _orig_generate_signals
from forex.strategy_donchian import should_exit as _orig_should_exit
from forex.strategy_donchian import size_position  # re-export unchanged

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


def _closed_past_stop(direction: str, close_val: float, stop_price: float) -> bool:
    """True if a given close has already breached the stop level in the
    direction that would trigger a hard stop."""
    if pd.isna(close_val) or pd.isna(stop_price):
        return False
    if str(direction).lower() in ("buy", "long"):
        return close_val <= stop_price
    else:
        return close_val >= stop_price


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wraps the original Donchian should_exit. The closed-trade ledger shows
    'hard_stop' is by far the dominant exit reason (18 of 35 quality trades)
    with only a 27.8% win rate (13 losses vs 5 wins) -- consistent with
    single-bar whipsaw stop-outs on daily closes near the ATR stop level.
    We require TWO consecutive daily closes past the ATR hard-stop level
    before honoring a hard_stop exit; a single-bar breach is deferred one
    bar to filter noise. All other exit reasons (Donchian trailing exit,
    time stop, etc.) pass through unchanged.
    """
    should_exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not should_exit_flag:
        return should_exit_flag, reason

    reason_lower = str(reason).lower()
    is_hard_stop_reason = ("hard_stop" in reason_lower) or ("stop-loss" in reason_lower) or ("stop_loss" in reason_lower)

    if not is_hard_stop_reason:
        return should_exit_flag, reason

    if df is None or len(df) < 2:
        # Not enough history to confirm -- fall back to original decision.
        return should_exit_flag, reason

    direction = position.get("direction", "")
    stop_price = position.get("stop_price", None)
    if stop_price is None:
        return should_exit_flag, reason

    closes = df["Close"]
    last_close = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2])

    last_breached = _closed_past_stop(direction, last_close, stop_price)
    prev_breached = _closed_past_stop(direction, prev_close, stop_price)

    if last_breached and prev_breached:
        # Two consecutive closes confirm the stop breach -- honor the exit.
        return True, reason

    # Single-bar breach only -- defer the hard stop one bar to avoid
    # whipsaw exits, matching the pattern seen in the loss-heavy hard_stop
    # bucket of the closed trade ledger.
    return False, "hard_stop_awaiting_confirmation"
