# AI-WRITTEN Phase 2+3 2026-09-08 by claude-sonnet-5
# Entry filter: block simultaneous entries that share a currency leg with an already-open EMA position (NZD-cross cascade)
# Exit filter: unchanged -- require the ATR hard-stop breach to hold for 2 consecutive daily closes before honoring the exit, to filter single-bar noise whipsaws

from typing import Set, Tuple

import pandas as pd

from forex.strategy_ema import generate_signals as _orig_generate_signals
from forex.strategy_ema import should_exit as _orig_should_exit


def _currency_pair(symbol: str) -> Tuple[str, str]:
    """Split a 6-char FX symbol like 'NZDAUD' into (base, quote) 3-letter codes.

    Falls back gracefully if the symbol format is unexpected (e.g. length != 6).
    """
    s = symbol.upper()
    if len(s) >= 6:
        return s[:3], s[3:6]
    return s, s


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap the original EMA crossover strategy with a currency-concentration cap.

    Trade history (52 closed trades) showed the overwhelming majority of losses
    (roughly 40 of 52 closed trades, essentially all hard_stop exits) clustered
    on NZDAUD, NZDSGD, NZDHKD and NZDUSD, opened and stopped out within minutes
    of each other in repeating cascades. All four symbols share NZD as a common
    currency leg -- a single underlying NZD move whipsawed every open NZD cross
    simultaneously, multiplying losses instead of diversifying them, and the
    strategy kept re-entering the same currency exposure through multiple
    correlated pairs at once.

    This filter blocks any new signal whose base or quote currency is already
    represented in an open position (from open_symbols), and also de-duplicates
    currency exposure within the same batch of new signals returned by the
    original strategy, so the book never holds more than one position per
    currency leg at a time. This does not change the underlying EMA/ADX logic
    or exits -- it only restricts which of the original signals get through.
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
        # Reserve these currencies so later signals in this same batch
        # (already sorted by score descending) can't also claim them.
        committed_currencies.add(base)
        committed_currencies.add(quote)

    return filtered


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wrap the original EMA should_exit with a 2-close confirmation on hard stops.

    Original Phase-3 rationale (52 closed trades, prior to this filter):
    49 of 52 exits were hard_stop, with only a 6.1%% win rate among those (46
    losses vs 3 wins) even though total P&L for that bucket was net positive
    thanks to a few large winners. That low win rate on a stop-loss exit
    indicated many stops were single-bar noise -- a brief spike through the
    ATR stop level that reversed immediately rather than a genuine sustained
    move against the position. The fix added here was: when the original
    strategy signals a hard-stop exit, require that the *previous* daily
    close had already breached the stop in the same direction before
    honoring the exit on the current bar; otherwise hold one more bar.

    Updated data (this review, 30 quality closed trades, all captured AFTER
    this confirmation filter was live) shows hard_stop win rate has risen to
    62.1%% (18 wins / 11 losses, avg P&L +22.4, total P&L +648.87), and the
    lone profit_target exit was also a winner. There is no exit_reason bucket
    in the current data with a high loss count and a low/zero win rate --
    the opposite problem from before -- so there is no new pattern here to
    correct. Per the evolution rules, when the data shows no clear new
    pattern the existing, already-validated exit logic is preserved
    unchanged rather than adding speculative rules on top of a small
    (n=30, single dominant bucket) sample.

    This wrapper does not change entries, sizing, or the crossover/time-stop
    exit paths. It only adds the previously-validated guard: when the
    original strategy signals an exit whose reason mentions "stop" (the ATR
    hard stop), we require that the *previous* daily close was already
    beyond the stop level in the same direction before honoring the exit on
    the current bar. If the previous close had not yet breached the stop, we
    hold the position one more bar (returning False with a
    'stop_confirmation_pending' note) to avoid exiting on a single-bar
    spike. If the original exit is for any other reason (crossover_reversal,
    time stop, profit_target, roster flatten, etc.), we pass it through
    unchanged.
    """
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_flag:
        return exit_flag, reason

    reason_lower = (reason or "").lower()
    if "stop" not in reason_lower:
        # Not a hard-stop exit (e.g. crossover_reversal, time stop, profit
        # target, roster flatten) -- leave the original decision untouched.
        return exit_flag, reason

    stop_price = position.get("stop_price")
    direction = str(position.get("direction", "")).lower()

    if stop_price is None or direction not in ("long", "short") or len(df) < 2 or "Close" not in df.columns:
        # Not enough information to confirm -- fall back to original decision.
        return exit_flag, reason

    prev_close = df["Close"].iloc[-2]

    if direction == "long":
        prev_breach = prev_close <= stop_price
    else:  # short
        prev_breach = prev_close >= stop_price

    if not prev_breach:
        # The stop has only been breached on the most recent bar -- hold one
        # more bar to confirm this isn't a single-bar spike/whipsaw.
        return False, "stop_confirmation_pending"

    return exit_flag, reason
