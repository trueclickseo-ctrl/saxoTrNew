# AI-WRITTEN Phase 2+3 2026-10-17 by claude-sonnet-5
# Entry filter: Filters out NZD-involved pairs, which caused ~71% of realized losses.
# Exit filter: Requires 2 consecutive daily closes past EMA(50) before honoring a trend_break exit, since single-bar trend_break exits were 14/15 losers (data unchanged this cycle -- rule left as-is).

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

    Exit-reason data (refreshed this cycle, 102 quality trades total) shows
    'trend_break' exits (close crosses EMA(50)) remain extremely poor: 15
    trades, 14 losses, only 1 win, avg -38.8 EUR/trade, total -582.43 EUR --
    by far the worst-performing exit bucket, and the numbers are IDENTICAL
    to the prior two cycle pulls. This is now the third consecutive cycle
    with the exact same trend_break stats, strongly suggesting either (a)
    no new trend_break exits have fired since the 2-bar filter was
    installed -- i.e. the filter is successfully deferring/suppressing bad
    single-bar whipsaw exits and those positions are resolving via other
    exit paths instead -- or (b) the underlying sample genuinely hasn't
    grown. Either way there is no new evidence to justify tightening,
    loosening, or replacing the rule, so it is kept exactly as-is per the
    Phase 3 guidance to avoid overfitting to a stale/unchanged sample.

    The rule: require the close to have been on the 'broken' side of
    EMA(50) for the last TWO consecutive closed bars before honoring a
    trend_break exit signal from the original strategy. A single-bar
    crossing looks like whipsaw, not a genuine trend reversal.

    Other exit-reason buckets were reviewed again this cycle and NOT
    touched:
      - hard_stop: 77 trades, 46.8% win rate, avg +0.2 EUR/trade --
        essentially breakeven with no directional bias to exploit; the
        1.5xATR stop distance still looks reasonably calibrated.
      - Six singleton 'STOP-LOSS hit @ <price>' rows (n=1 each, all
        losses, -562 EUR combined) -- each a distinct symbol/price with
        sample size of one; too sparse to distinguish systematic gap-
        through-stop risk from random bad luck. No rule added to avoid
        overfitting to single instances.
      - roster_flatten_2026-09-02: forced administrative exits (n=5, mixed
        result, +330 EUR net), not a strategy-driven signal -- left
        untouched.
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
