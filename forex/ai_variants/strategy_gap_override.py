# AI-WRITTEN Phase 2+3 2026-09-25 by claude-sonnet-5
# Entry filter: none (pass-through) -- Phase 2 blacklist (NOK/SEK/DKK/PLN/CZK) removed; Nordic/Scandi crosses profitable, insufficient data to re-filter
# Exit filter: Require 2 consecutive daily closes beyond the hard-stop level before confirming a hard-stop exit (UNCHANGED this cycle -- identical ledger snapshot to prior cycle, no new closed trades since rule went live)

import pandas as pd
from forex.strategy_gap import generate_signals as _orig_generate_signals
from forex.strategy_gap import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None,
                     live_prices: dict = None,
                     exhausted_symbols: set = None, **kwargs) -> list:
    """Pass-through -- no entry filter applied yet (insufficient data)."""
    return _orig_generate_signals(
        market_data,
        open_symbols=open_symbols,
        live_prices=live_prices,
        exhausted_symbols=exhausted_symbols,
        **kwargs,
    )


def _is_hard_stop_reason(reason: str) -> bool:
    """Identify hard-stop exit reasons while excluding time_stop (which
    also contains the substring 'stop').
    """
    if not reason:
        return False
    r = reason.lower()
    if "time" in r:
        return False
    return ("hard_stop" in r) or ("stop-loss" in r) or (r == "stop")


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wraps strategy_gap.should_exit, adding a 2-bar close confirmation
    requirement before honoring a hard-stop exit signal.

    Rationale (from closed-trade ledger, 249 quality trades, re-verified in
    this Phase 3 pass against the SAME exit_reason breakdown that was used
    to originally build this rule): hard_stop exits account for 59 trades
    with only 1 win (1.7% win rate) and -4076.5 total P&L -- by far the
    single largest loss bucket in the strategy (time_stop is roughly
    break-even at -306.23/50 trades, and gap_filled is net profitable at
    +12244.38/79 trades despite a lower per-trade win rate, because winners
    run further than losers). Because this is a mean-reversion gap-fade
    strategy, a single-bar wick touching the stop level does not
    necessarily mean the adverse move will persist. We therefore require
    the CLOSE to have breached the stop level on the current AND the
    immediately preceding bar before confirming the exit. A single-bar
    breach is treated as 'pending confirmation' and the position is held
    one more bar; the broker-side protective stop-loss order (set at
    position entry) still guards against catastrophic moves regardless of
    this wrapper -- this logic only affects the strategy's own
    should_exit() early-exit decision path, not the hard protective stop
    order itself.

    This Phase 3 pass re-examined the exit_reason breakdown supplied for
    the current evolution cycle and confirmed it is numerically identical
    to the prior cycle's snapshot (same 59/1/-4076.5 hard_stop signature,
    same near-flat time_stop bucket, same profitable gap_filled bucket).
    Since no new closed-trade data has accumulated since the confirmation
    rule was added, there is still no evidence on whether the rule reduced
    hard_stop losses in live/paper trading going forward. Making a further
    change (e.g. adding a percentage-distance buffer on top of the 2-bar
    close requirement) would not be backed by any NEW data point and risks
    over-fitting to a single already-addressed pattern. The rule is
    therefore left UNCHANGED this cycle, pending a fresh ledger pull that
    reflects trades closed after the confirmation logic went live. The
    large number of single-trade 'STOP-LOSS hit @ <price>' /
    'TAKE-PROFIT hit @ <price>' buckets remain broker-side synthetic exit
    tags (unique price per trade, n=1 or 2 each) too sparse individually
    to support any additional targeted rule beyond the aggregate
    hard_stop / gap_filled / time_stop buckets already covered.
    """
    should_exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not should_exit_flag:
        return should_exit_flag, reason

    if not _is_hard_stop_reason(reason):
        return should_exit_flag, reason

    if df is None or len(df) < 2:
        # Not enough history to confirm -- fall back to original decision.
        return should_exit_flag, reason

    stop_price = position.get("stop_price")
    direction = str(position.get("direction", "")).lower()

    if stop_price is None or direction not in ("long", "buy", "short", "sell"):
        return should_exit_flag, reason

    try:
        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])
    except (KeyError, IndexError, ValueError, TypeError):
        return should_exit_flag, reason

    if direction in ("long", "buy"):
        breached_now = last_close < stop_price
        breached_prev = prev_close < stop_price
    else:
        breached_now = last_close > stop_price
        breached_prev = prev_close > stop_price

    if breached_now and not breached_prev:
        # Only one bar has closed beyond the stop -- wait for confirmation
        # rather than exiting on a possible single-bar wick/noise event.
        return False, "hard_stop_pending_confirmation"

    return should_exit_flag, reason
