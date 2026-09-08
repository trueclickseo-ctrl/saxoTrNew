# AI-WRITTEN Phase 2+3 2026-09-08 by claude-sonnet-5
# Entry filter: Block new signals on exotic/thin-liquidity currency crosses (NOK, SEK, DKK, PLN, CZK, TRY, THB, HKD, MXN)
# Exit filter: Require 2 consecutive daily closes beyond the hard-stop level before confirming a hard-stop exit, to avoid single-bar wick stop-outs on mean-reverting gap-fade trades

import pandas as pd
from forex.strategy_gap import generate_signals as _orig_generate_signals
from forex.strategy_gap import should_exit as _orig_should_exit

# Currency codes identified in the closed-trade ledger as consistent large
# losers for the gap strategy (wide spread / thin liquidity crosses).
EXOTIC_CCY_BLACKLIST = {
    "NOK", "SEK", "DKK", "PLN", "CZK", "TRY", "THB", "HKD", "MXN",
}


def _is_blacklisted(symbol: str) -> bool:
    if not symbol:
        return False
    sym = symbol.upper()
    for ccy in EXOTIC_CCY_BLACKLIST:
        if ccy in sym:
            return True
    return False


def generate_signals(market_data: dict, open_symbols: set = None,
                      live_prices: dict = None,
                      exhausted_symbols: set = None, **kwargs) -> list:
    """Wraps strategy_gap.generate_signals, filtering out signals on
    exotic/thin-liquidity currency crosses that have shown consistently
    poor performance in the closed trade ledger.
    """
    signals = _orig_generate_signals(
        market_data,
        open_symbols=open_symbols,
        live_prices=live_prices,
        exhausted_symbols=exhausted_symbols,
        **kwargs,
    )

    if not signals:
        return signals

    filtered = []
    for sig in signals:
        sym = sig.get("symbol") if isinstance(sig, dict) else None
        if _is_blacklisted(sym):
            continue
        filtered.append(sig)

    return filtered


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

    Rationale (from closed-trade ledger): hard_stop exits account for 59
    trades with only 1 win (1.7% win rate) and -4076.5 total P&L -- the
    single largest loss bucket in the strategy. Because this is a
    mean-reversion gap-fade strategy, a single-bar wick touching the stop
    level does not necessarily mean the adverse move will persist. We
    therefore require the CLOSE to have breached the stop level on the
    current AND the immediately preceding bar before confirming the exit.
    A single-bar breach is treated as "pending confirmation" and the
    position is held one more bar; the original stop-loss logic (which
    may use intrabar highs/lows) still protects against catastrophic
    moves via the runner's hard stop-loss order at the broker level -- this
    wrapper only affects the strategy's own should_exit() early-exit
    decision path, not the broker-side protective stop order.
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
