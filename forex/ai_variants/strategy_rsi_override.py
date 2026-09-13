# AI-WRITTEN Phase 2+3 2026-09-19 by claude-sonnet-5
# Entry filter: exclude JPY-cross symbols (drove almost all hard-stop losses in SIM sample)
# Exit filter: require 2 consecutive closes beyond stop_price before confirming a hard-stop exit (filter single-bar wick stop-outs)

import pandas as pd
import numpy as np

from forex.strategy_rsi import generate_signals as _orig_generate_signals
from forex.strategy_rsi import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap the original RSI(2) pullback generator with a JPY-cross exclusion filter.

    Evidence (100 closed SIM trades):
      - Every hard_stop exit (e.g. HUFJPY, ZARJPY x2, JPYNOK, MXNJPY x2,
        CNHJPY, AUDJPY, NOKJPY, and other JPY-quoted/based crosses) and
        every raw 'STOP-LOSS hit' exit landed on a symbol with JPY as
        base or quote currency. These made up the large majority of
        losing trades and drove the strategy's overall PF to 0.53 and
        win rate to 38% despite rsi_recovery exits on non-JPY pairs
        (DKKPLN, EURGBP, GBPPLN, USDPLN, EURDKK, etc.) being solidly
        profitable in aggregate.
      - This points to JPY-cross volatility/wick behaviour causing this
        system's 1.5xATR stop to be tagged far more often than on other
        FX pairs, while the identical RSI(2)/EMA(200) entry logic
        performs acceptably elsewhere.

    We therefore filter JPY-involved symbols out of the entry signal
    list entirely, leaving the original strategy's ranking/logic
    untouched for all other pairs. Exit logic (should_exit) is passed
    through unmodified.
    """
    signals = _orig_generate_signals(market_data, open_symbols, **kwargs)
    filtered = [s for s in signals if "JPY" not in s.get("symbol", "")]
    return filtered


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wrap the original should_exit() with a stop-confirmation filter.

    Evidence (169 quality closed SIM trades):
      - 'hard_stop' exits: 43 trades, only 1 win (2.3% win rate), total
        pnl -2360.05, avg -54.90/trade.
      - An additional 10 individual 'STOP-LOSS hit @<price>' exit_reasons
        (one trade each) were ALL losses, totalling roughly -757 pnl.
      - Combined, stop-triggered exits account for 53 of 169 trades
        (~31%) and are responsible for the overwhelming majority of
        this strategy's realized losses, versus rsi_recovery exits
        (83 trades, 63.9% win rate, +1687.13 total pnl) which are
        healthy and are left untouched here.
      - Daily FX bars can wick sharply intrabar (especially around
        thin-liquidity crosses) and tag a stop level on High/Low even
        when the Close for that same bar, and often the next bar too,
        recovers back inside the stop. A single-bar wick-triggered stop
        gives the market no chance to prove the break is real.

    Change: when the wrapped should_exit() signals an exit AND the
    reported reason is stop-related (hard_stop / 'STOP-LOSS hit ...'),
    we require CONFIRMATION: both the current bar's Close and the prior
    bar's Close must already be beyond position['stop_price'] (in the
    adverse direction) before we honour the exit. If not yet confirmed,
    we hold the position one more bar (return False) rather than exit
    on a single wick-only stop tag. rsi_recovery and time-stop exits are
    passed through completely unmodified, since the data shows those
    are working well as-is.

    NOTE: this does not touch the actual stop_price value or the
    runner's ratcheting ladder -- it only adds a confirmation gate on
    top of the ORIGINAL should_exit()'s stop-hit decision, and only for
    that one exit category.
    """
    exit_now, reason = _orig_should_exit(position, df, calendar_days_held)

    if not exit_now:
        return exit_now, reason

    reason_str = reason if isinstance(reason, str) else ""
    reason_l = reason_str.lower()
    is_time_stop = "time" in reason_l or "day" in reason_l
    is_rsi_exit = "rsi" in reason_l
    is_stop_reason = ("stop" in reason_l) and not is_time_stop and not is_rsi_exit

    if is_stop_reason:
        stop_price = position.get("stop_price")
        direction = str(position.get("direction", "")).lower()

        if stop_price is not None and df is not None and len(df) >= 2 and "Close" in df.columns:
            closes = df["Close"]
            try:
                last_close = float(closes.iloc[-1])
                prev_close = float(closes.iloc[-2])
                stop_price = float(stop_price)
            except (TypeError, ValueError):
                return exit_now, reason

            if direction == "long":
                confirmed = (last_close <= stop_price) and (prev_close <= stop_price)
            elif direction == "short":
                confirmed = (last_close >= stop_price) and (prev_close >= stop_price)
            else:
                confirmed = True

            if not confirmed:
                return False, "stop_wick_unconfirmed_hold"

    return exit_now, reason
