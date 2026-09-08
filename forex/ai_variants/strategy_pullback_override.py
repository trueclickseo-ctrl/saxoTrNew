# AI-WRITTEN Phase 2+3 2026-09-26 by claude-sonnet-5
# Entry filter: Filters out NZD-involved pairs, which caused ~71% of realized losses.
# Exit filter: Requires 2 consecutive daily closes past EMA(50) before honoring a trend_break exit, since single-bar trend_break exits were 14/15 losers (unchanged this cycle -- see rationale).

import pandas as pd
import numpy as np
from forex import strategy_pullback as _orig
from forex.strategy_pullback import should_exit as _orig_should_exit


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap original pullback generate_signals, filtering out NZD-involved pairs.

    Trade history shows NZD-quoted/based crosses (NZDHKD, NZDUSD, NZDSGD,
    NZDPLN, NZDCNH) produced the vast majority of realized losses for this
    strategy (~71% of total -1101 EUR pnl from just 14/35 trades), including
    two catastrophic hard-stop losses on the same roster-entry timestamp.
    This filter removes any signal whose symbol contains 'NZD'.
    """
    signals = _orig.generate_signals(market_data, open_symbols=open_symbols, **kwargs)

    filtered = []
    for sig in signals:
        sym = sig.get("symbol", "") if isinstance(sig, dict) else getattr(sig, "symbol", "")
        if "NZD" in sym.upper():
            continue
        filtered.append(sig)

    return filtered


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _is_trend_break_reason(reason: str) -> bool:
    if not reason:
        return False
    r = reason.lower()
    return "trend_break" in r or "trend break" in r


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wrap original should_exit, adding a 2-bar confirmation filter for trend_break exits.

    Exit-reason data (both the original diagnosis and this cycle's refreshed
    ledger pull) shows 'trend_break' exits (close crosses EMA(50)) are
    extremely poor: 15 trades, 14 losses, only 1 win, avg -38.8 EUR/trade,
    total -582 EUR -- by far the worst-performing exit bucket. This looks
    like single-bar whipsaw across EMA(50) rather than a genuine trend
    reversal. We require the close to have been on the 'broken' side of
    EMA(50) for the last TWO consecutive closed bars before honoring a
    trend_break exit signal from the original strategy.

    This cycle's ledger pull shows IDENTICAL trend_break stats to the prior
    cycle (n=15, 14 losses, win_rate 6.7%, avg -38.8, total -582.43) --
    meaning no new trend_break exits have occurred since the 2-bar filter
    was installed (the filter is presumably suppressing/deferring them, or
    none have recurred yet). Since the underlying data hasn't moved, the
    2-bar confirmation rule is left exactly as-is rather than tightened
    further off the same static sample.

    Other exit-reason buckets were reviewed this cycle and NOT touched:
      - hard_stop: 74 trades, 44.6% win rate, avg +0.1 EUR/trade -- roughly
        breakeven with no directional bias to exploit; the 1.5xATR stop
        distance appears reasonably calibrated already.
      - Six singleton 'STOP-LOSS hit @ <price>' rows (n=1 each, all
        losses, -562 EUR combined) -- each is a distinct symbol/price with
        a sample size of one. This is too sparse to distinguish a genuine
        systematic flaw (e.g. gap-through stops) from random bad luck, and
        adding a rule tuned to single-instance observations would be
        overfitting. No change made.
      - roster_flatten: forced administrative exits, not a strategy signal
        -- left untouched.
    """
    orig_exit, orig_reason = _orig_should_exit(position, df, calendar_days_held)

    if not orig_exit or not _is_trend_break_reason(orig_reason):
        return orig_exit, orig_reason

    # Need at least 2 closed bars plus EMA(50) history to confirm.
    if df is None or len(df) < 52:
        return orig_exit, orig_reason

    closes = df["Close"]
    ema50 = _ema(closes, 50)

    direction = str(position.get("direction", "")).lower()

    last_close = closes.iloc[-1]
    prev_close = closes.iloc[-2]
    last_ema = ema50.iloc[-1]
    prev_ema = ema50.iloc[-2]

    if direction in ("long", "buy"):
        # Trend break for longs = close falls below EMA50.
        last_broken = last_close < last_ema
        prev_broken = prev_close < prev_ema
    elif direction in ("short", "sell"):
        # Trend break for shorts = close rises above EMA50.
        last_broken = last_close > last_ema
        prev_broken = prev_close > prev_ema
    else:
        # Unknown direction encoding -- don't second-guess the original logic.
        return orig_exit, orig_reason

    if last_broken and prev_broken:
        # Two consecutive confirmed bars -- honor the exit.
        return True, orig_reason

    # Only a single-bar break -- historically these were 14/15 losers.
    # Block the early exit and let the position ride (hard stop / time stop
    # still apply on subsequent bars via the original should_exit call).
    return False, "trend_break_awaiting_2bar_confirmation"
