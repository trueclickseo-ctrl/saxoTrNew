# AI override for gap_quality (pass-through).
# gap_quality is itself the EXOTIC-filtered version of gap; no further
# override is needed. This file exists so ai_sim's override loader finds
# a module and doesn't fall back to the base strategy.
# The 2-bar hard-stop confirmation from the gap override is also applied
# here (gap_quality shares the same exit logic as gap via should_exit).

import pandas as pd
from forex.strategy_gap_quality import generate_signals as _orig_generate_signals
from forex.strategy_gap_quality import generate_session_signals as _orig_generate_session_signals
from forex.strategy_gap_quality import should_exit as _orig_should_exit


def generate_signals(*args, **kwargs):
    return _orig_generate_signals(*args, **kwargs)


def generate_session_signals(*args, **kwargs):
    return _orig_generate_session_signals(*args, **kwargs)


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """2-bar hard-stop confirmation — same rule as gap override Phase 2+3."""
    should_exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not should_exit_flag:
        return should_exit_flag, reason

    reason_lower = (reason or "").lower()
    if "time" in reason_lower or "gap_filled" in reason_lower:
        return should_exit_flag, reason
    if not (("hard_stop" in reason_lower) or ("stop-loss" in reason_lower)):
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
        breached_prev = prev_close < stop_price
    else:
        breached_prev = prev_close > stop_price

    if not breached_prev:
        return False, "hard_stop_pending_confirmation"

    return should_exit_flag, reason
