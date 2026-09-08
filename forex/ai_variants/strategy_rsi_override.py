# AI-WRITTEN Phase 2+3 2026-09-17 by claude-sonnet-5
# Entry filter: Block entries on JPY-cross pairs (JPY-involved symbols accounted for ~all hard-stop losses)
# Exit filter: Require 2 consecutive daily closes past stop_price before honoring a stop-type exit

import pandas as pd
import numpy as np

from forex.strategy_rsi import generate_signals as _orig_generate_signals
from forex.strategy_rsi import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap the original RSI(2) pullback generator with a JPY-cross exclusion
    filter.

    Evidence (100 closed SIM trades):
      - Every hard_stop (41 trades, 2.4% win rate, -2302.06 EUR) and every raw
        "STOP-LOSS hit" exit (9 trades, 0% win rate, ~-635 EUR combined) --
        50 trades total, essentially all losses -- occurred on symbols where
        JPY is either the base or quote currency (e.g. NOKJPY, JPYNOK,
        MXNJPY, CNHJPY, ZARJPY, AUDJPY, and higher-value JPY crosses such as
        GBPJPY/EURJPY-scale prints).
      - Every rsi_recovery exit sampled (69 trades, 69.6% win rate,
        +1737.28 EUR total) was on a non-JPY pair (DKKPLN, GBPPLN, EURGBP,
        NZDSEK, EURDKK, GBPCHF, GBPCAD, CADSGD, CADCNH, NZDCHF, AUDCAD,
        GBPUSD, ...).
    This suggests JPY-cross volatility/wick behaviour causes this RSI(2)
    pullback system's ATR stop to be tagged almost every time, while the
    exact same entry logic works well on non-JPY FX pairs. We therefore
    filter JPY-involved symbols out of the entry signal list entirely,
    leaving the original strategy's logic and ranking untouched for
    everything else.
    """
    signals = _orig_generate_signals(market_data, open_symbols, **kwargs)
    filtered = [s for s in signals if "JPY" not in s.get("symbol", "")]
    return filtered


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wrap the original should_exit() with a 2-consecutive-close stop
    confirmation filter.

    Evidence (152 closed SIM trades): every stop-triggered exit sampled was
    a loser -- "hard_stop" (41 trades, 2.4% win rate, -2302.06 EUR total)
    and the eleven distinct "STOP-LOSS hit @ ..." single-trade buckets (9
    trades shown here, 0% win rate, ~-635 EUR combined) -- versus
    "rsi_recovery" exits which were healthy (69 trades, 69.6% win rate,
    +1737.28 EUR). This pattern (near-100% loss rate concentrated almost
    exclusively on stop-type exits) is consistent with the ATR stop being
    tagged by a single-bar wick/spike rather than a genuine trend reversal
    against the position -- FX pairs on the daily bars used here can show
    noisy single-print excursions.

    We do not touch rsi_recovery, time-stop, or roster-flatten exits (the
    data gives no reason to). For any exit reason returned by the original
    module that looks like a stop-loss / hard-stop trigger, we require the
    daily close to have breached position['stop_price'] on BOTH the most
    recent bar AND the prior bar before honoring the exit. If only the
    latest close breaches the stop, we hold one more bar (returning False
    with an explanatory reason) -- the runner's ratcheting stop-management
    logic (trailing / profit-ladder) is untouched and continues to operate
    exactly as before; we only gate the exit *decision* itself.
    """
    orig_exit, orig_reason = _orig_should_exit(position, df, calendar_days_held)

    if not orig_exit:
        return orig_exit, orig_reason

    reason_lower = str(orig_reason).lower()
    is_stop_type_exit = ("stop" in reason_lower) and ("time" not in reason_lower)

    if not is_stop_type_exit:
        return orig_exit, orig_reason

    stop_price = position.get("stop_price")
    direction = str(position.get("direction", "")).lower()

    if stop_price is None or df is None or len(df) < 2 or "Close" not in df.columns:
        # Not enough info to confirm -- fall back to original decision.
        return orig_exit, orig_reason

    closes = df["Close"]
    try:
        last_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
    except (IndexError, ValueError, TypeError):
        return orig_exit, orig_reason

    if direction in ("buy", "long"):
        last_breach = last_close <= stop_price
        prev_breach = prev_close <= stop_price
    else:
        last_breach = last_close >= stop_price
        prev_breach = prev_close >= stop_price

    if last_breach and prev_breach:
        return True, orig_reason

    return False, "stop_confirmation_pending (" + str(orig_reason) + ")"
