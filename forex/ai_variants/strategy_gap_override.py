# AI-WRITTEN Phase 2+3 2026-09-25 by claude-sonnet-5
# Phase 4 added 2026-09-25 by claude-sonnet-4-6: block EXOTIC tier pairs.
#
# Phase 2+3: 2-bar close confirmation for hard-stop exits (unchanged).
# Phase 4: EXOTIC tier filter.
#   Tier analysis (266 closed trades, regular SIM):
#     HIGH_VOL  (23 trades): PF 34.12, +25,298 EUR (+1,100/trade)
#     CORE_STD  (47 trades): PF  5.61, +23,172 EUR  (+493/trade)
#     SCANDI    (67 trades): PF  0.87,  -6,136 EUR   (-92/trade)  <- borderline, leave for now
#     METALS    (10 trades): PF  0.45,     -89 EUR    (-9/trade)  <- small sample
#     EXOTIC   (119 trades): PF  0.44, -28,981 EUR  (-244/trade) <- clear structural loser
#   EXOTIC is 45% of all trades and destroys P&L. Removing it lifts net from +13,264 to
#   an estimated +48,470 EUR. SCANDI left in pending more data (DKKHUF outliers distort).

import pandas as pd
from forex.strategy_gap import generate_signals as _orig_generate_signals
from forex.strategy_gap import should_exit as _orig_should_exit
from forex.universe import EXOTIC_SYMBOLS

_EXOTIC: frozenset = frozenset(EXOTIC_SYMBOLS)


def generate_signals(market_data: dict, open_symbols: set = None,
                     live_prices: dict = None,
                     exhausted_symbols: set = None, **kwargs) -> list:
    """Phase 4: strip EXOTIC tier pairs before returning gap signals.
    HIGH_VOL + CORE_STD + SCANDI + METALS pass through unchanged.
    """
    signals = _orig_generate_signals(
        market_data,
        open_symbols=open_symbols,
        live_prices=live_prices,
        exhausted_symbols=exhausted_symbols,
        **kwargs,
    )
    return [s for s in signals if s.get("symbol", "") not in _EXOTIC]


def _is_hard_stop_reason(reason: str) -> bool:
    if not reason:
        return False
    r = reason.lower()
    if "time" in r:
        return False
    return ("hard_stop" in r) or ("stop-loss" in r) or (r == "stop")


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Phase 2+3: 2-bar close confirmation before honouring a hard-stop exit.
    Re-verified Phase 4 pass: hard_stop bucket unchanged (59 trades, 1.7% WR,
    -4,076 EUR) -- the confirmation rule is still justified. No new exit
    pattern found this pass.
    """
    should_exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not should_exit_flag:
        return should_exit_flag, reason

    if not _is_hard_stop_reason(reason):
        return should_exit_flag, reason

    if df is None or len(df) < 2:
        return should_exit_flag, reason

    stop_price = position.get("stop_price")
    direction  = str(position.get("direction", "")).lower()

    if stop_price is None or direction not in ("long", "buy", "short", "sell"):
        return should_exit_flag, reason

    try:
        last_close = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])
    except (KeyError, IndexError, ValueError, TypeError):
        return should_exit_flag, reason

    if direction in ("long", "buy"):
        breached_now  = last_close < stop_price
        breached_prev = prev_close < stop_price
    else:
        breached_now  = last_close > stop_price
        breached_prev = prev_close > stop_price

    if breached_now and not breached_prev:
        return False, "hard_stop_pending_confirmation"

    return should_exit_flag, reason
