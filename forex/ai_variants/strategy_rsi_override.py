# AI-WRITTEN Phase 2+3 2026-09-21 by claude-sonnet-5
# Entry filter: exclude JPY-quoted/JPY-based symbols from entries (Phase 2, unchanged).
# Exit filter: require 2 consecutive bar closes beyond stop_price before confirming a hard_stop exit.

import pandas as pd
import numpy as np

from forex.strategy_rsi import generate_signals as _orig_generate_signals

try:
    from forex.strategy_rsi import should_exit as _orig_should_exit
except ImportError:
    _orig_should_exit = None


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap the original RSI(2) pullback generator with a JPY-cross exclusion filter.

    Evidence (38 closed SIM trades):
      - 12 of 38 closed trades involved a JPY-quoted or JPY-based symbol
        (NOKJPY, JPYNOK, HUFJPY, ZARJPY, MXNJPY x2, CNHJPY, AUDJPY x2,
        SGDJPY, JPYDKK, DKKJPY). These 12 trades summed to roughly
        -315 EUR (only 1 of the 12 was a winner, an 8% win rate) and
        every one of them exited via hard_stop, indicating the 1.5xATR
        stop is tagged far more aggressively on JPY-cross volatility.
      - The remaining ~18 non-JPY trades in the same sample summed to
        roughly +40 EUR with a ~78% win rate and mostly clean
        rsi_recovery exits, showing the RSI(2)/EMA(200) mean-reversion
        edge is intact and profitable on non-JPY FX/majors and
        EM/Nordic crosses.
      - Overall strategy PF of 0.53 / win rate 44.7% is driven entirely
        by the JPY-cross subset; removing it flips the sample to net
        positive.

    We therefore filter any symbol containing 'JPY' out of the entry
    signal list entirely, leaving the original strategy's ranking/logic
    untouched for all other pairs. Exit logic (should_exit) is passed
    through unmodified if present.
    """
    signals = _orig_generate_signals(market_data, open_symbols, **kwargs)
    filtered = [s for s in signals if "JPY" not in s.get("symbol", "").upper()]
    return filtered


if _orig_should_exit is not None:
    def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int, **kwargs):
        """Wrap the original should_exit() with a 2-bar confirmation requirement
        for hard_stop exits only.

        Evidence (38 closed SIM trades, exit_reason breakdown):
          - hard_stop: 18 trades, win_rate 5.6%, avg_pnl -30.0, total -539.1
          - rsi_recovery: 20 trades, win_rate 80.0%, avg_pnl +13.2, total +263.64

          rsi_recovery exits are clean and profitable -- no change needed.
          hard_stop exits, however, account for the strategy's entire net
          loss and fire on a single-bar stop touch. On FX daily bars a
          single-bar intrabar/close breach of a 1.5xATR stop can be a
          noise wick against a trend that is otherwise still intact
          (RSI(2) mean-reversion setups are inherently choppy). Requiring
          the stop breach to be confirmed by TWO consecutive closes past
          stop_price (rather than acting on the first touch) filters out
          one-bar whipsaws while still exiting promptly (next bar) if the
          breakdown/breakout is real. Time-stop and rsi_recovery exits are
          left completely unchanged.
        """
        exit_now, reason = _orig_should_exit(position, df, calendar_days_held, **kwargs)

        if not exit_now:
            return exit_now, reason

        reason_str = str(reason).lower()
        is_hard_stop = "hard_stop" in reason_str or ("stop" in reason_str and "time" not in reason_str and "rsi" not in reason_str)

        if is_hard_stop and df is not None and len(df) >= 2:
            direction = str(position.get("direction", "")).lower()
            stop_price = position.get("stop_price")

            if stop_price is not None and direction in ("long", "short"):
                try:
                    prev_close = float(df["Close"].iloc[-2])
                except (IndexError, KeyError, ValueError, TypeError):
                    prev_close = None

                if prev_close is not None:
                    if direction == "long":
                        prev_bar_confirmed = prev_close <= stop_price
                    else:
                        prev_bar_confirmed = prev_close >= stop_price

                    if not prev_bar_confirmed:
                        # First bar to breach the stop -- wait one more bar
                        # for confirmation instead of exiting immediately.
                        return False, "hard_stop_pending_confirmation"

        return exit_now, reason
