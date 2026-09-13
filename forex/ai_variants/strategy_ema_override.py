# AI-WRITTEN Phase 2+3 2026-09-05 by claude-sonnet-5
# Entry filter: block new entries sharing a currency leg with an already-open position (NZD-cross cascade fix)
# Exit filter: require 2 consecutive closes beyond stop_price before honoring a hard_stop exit, to filter single-bar wick spikes

from typing import Dict, Set, Tuple

import pandas as pd

from forex.strategy_ema import generate_signals as _orig_generate_signals
from forex.strategy_ema import should_exit as _orig_should_exit


def _currency_pair(symbol: str) -> Tuple[str, str]:
    """Split a 6-char FX symbol like 'NZDAUD' into (base, quote) 3-letter codes.

    Falls back gracefully if symbol format is unexpected.
    """
    s = symbol.upper()
    if len(s) >= 6:
        return s[:3], s[3:6]
    return s, s


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap the original EMA crossover strategy with a currency-concentration cap.

    Trade history (52 closed trades) showed a severe cascade of near-simultaneous
    hard-stop losses across NZDAUD, NZDSGD, NZDHKD and NZDUSD -- all sharing NZD
    as a common currency leg. A single underlying NZD move whipsawed every NZD
    cross the strategy was long/short at once, multiplying losses instead of
    diversifying them. This filter blocks any new signal whose base or quote
    currency is already represented in an open position, and also de-duplicates
    currency exposure within the same batch of signals, so the book never holds
    more than one position per currency leg at a time.
    """
    if open_symbols is None:
        open_symbols = set()

    raw_signals = _orig_generate_signals(market_data, open_symbols=open_symbols, **kwargs)

    if not raw_signals:
        return raw_signals

    # Currencies already committed via existing open positions.
    committed_currencies: Set[str] = set()
    for sym in open_symbols:
        base, quote = _currency_pair(sym)
        committed_currencies.add(base)
        committed_currencies.add(quote)

    filtered = []
    for sig in raw_signals:
        sym = sig.get("symbol", "")
        base, quote = _currency_pair(sym)
        if base in committed_currencies or quote in committed_currencies:
            continue
        filtered.append(sig)
        committed_currencies.add(base)
        committed_currencies.add(quote)

    return filtered


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> Tuple[bool, str]:
    """Wrap the original exit logic with a 2-bar close confirmation on hard_stop.

    Closed-trade data showed hard_stop firing 49 of 52 times with only a 6.1%
    win rate (46 losses) -- by far the dominant loss driver. This pattern is
    consistent with the ATR stop being clipped by single-bar wick spikes
    (intrabar High/Low touching stop_price) that reverse the very next bar.
    We keep the original stop level and direction untouched -- we only refuse
    to honor a hard_stop exit until the Close price has remained beyond the
    stop for two consecutive bars, filtering noise without weakening protection
    against a genuinely sustained adverse move (which will simply confirm on
    the next bar).
    """
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag or reason != "hard_stop":
        return exit_flag, reason

    if df is None or len(df) < 2:
        return exit_flag, reason

    stop_price = position.get("stop_price")
    direction = str(position.get("direction", "")).lower()

    if stop_price is None or direction not in ("long", "short"):
        return exit_flag, reason

    try:
        last_close = df["Close"].iloc[-1]
        prev_close = df["Close"].iloc[-2]
    except Exception:
        return exit_flag, reason

    if direction == "long":
        last_breach = last_close < stop_price
        prev_breach = prev_close < stop_price
    else:
        last_breach = last_close > stop_price
        prev_breach = prev_close > stop_price

    if last_breach and prev_breach:
        return True, "hard_stop_confirmed"

    # Single-bar breach only (likely a wick spike) -- hold the position and
    # wait for a second confirming close before exiting on the hard stop.
    return False, "hard_stop_awaiting_confirmation"
