# AI-WRITTEN Phase 2+3 2026-10-24 by claude-sonnet-5
# Phase 4 added 2026-09-25 by claude-sonnet-4-6: block EXOTIC tier pairs.
#
# Entry filter: NZD-pairs + DKKJPY blocked (prior cycles). EXOTIC tier blocked (Phase 4).
# Exit filter: Tightens trend_break exit to require a 2-bar close-beyond-EMA50 confirmation WITH an ATR buffer.
#
# PENDING ACTION FOR EVOLVER (do not remove this comment until actioned):
# When closed organic trades from 2026-09-18 onwards >= 50 AND PF >= 1.2:
# reassess pair-level filters — JPY block committed 2026-09-18 (2910f47), NZD block
# committed 2026-09-05. Count only clean post-block trades. If PF >= 1.2 confirmed,
# flag in evolver report as live candidate for human review. Human decision logged 2026-09-18.
#
# Phase 4: EXOTIC tier filter.
#   Tier analysis (98 closed trades, regular SIM):
#     SCANDI    (26 trades): PF 3.82,  +300 EUR  (+11.5/trade) <- only profitable tier
#     HIGH_VOL  (22 trades): PF 0.67,  -333 EUR  (-15.1/trade)
#     CORE_STD  (12 trades): PF 0.75,   -72 EUR   (-6.0/trade)
#     EXOTIC    (45 trades): PF 0.27,  -889 EUR  (-19.8/trade) <- clear structural loser
#   EXOTIC is 46% of all trades and delivers -889 EUR (-19.8/trade).
#   Removing EXOTIC rescues net from -994 → approx -105 EUR.
#   EXOTIC losses are driven by thin EM-cross pairs where pullback retests
#   fail to hold due to wide spreads and event-driven gap risk.

import pandas as pd
import numpy as np
from forex import strategy_pullback as _orig
from forex.strategy_pullback import should_exit as _orig_should_exit
from forex.universe import EXOTIC_SYMBOLS

EXCLUDED_SUBSTR = ("NZD",)          # retained from prior cycle
EXCLUDED_EXACT = ("DKKJPY",)        # retained from prior cycle

_EXOTIC: frozenset = frozenset(EXOTIC_SYMBOLS)

# New this cycle: require the trend_break confirmation bars to close beyond EMA50 by at
# least this many ATRs (not just a bare cross) before honoring the exit. This adds
# hysteresis around the EMA50 line to reduce whipsaw trend_break exits.
TREND_BREAK_ATR_BUFFER = 0.25


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Wrap original pullback generate_signals.

    Filters applied:
      1. (retained) Skip any symbol containing 'NZD' -- historically ~71% of losses.
      2. (retained) Skip DKKJPY specifically — repeated whipsaw stop-outs at ~24.x.
      3. (Phase 4) Skip all EXOTIC tier pairs — 45 trades, -889 EUR (-19.8/trade),
         46% of all pullback trades. Thin EM-cross pairs where retest levels fail due
         to wide spreads and event-driven gap risk.
    """
    signals = _orig.generate_signals(market_data, open_symbols=open_symbols, **kwargs)

    filtered = []
    for sig in signals:
        sym = sig.get("symbol", "") if isinstance(sig, dict) else getattr(sig, "symbol", "")
        sym_u = sym.upper()
        if any(tag in sym_u for tag in EXCLUDED_SUBSTR):
            continue
        if sym_u in EXCLUDED_EXACT:
            continue
        if sym_u in _EXOTIC:
            continue
        filtered.append(sig)

    return filtered


def _is_trend_break_reason(reason: str) -> bool:
    if not reason:
        return False
    r = reason.lower()
    return "trend_break" in r or "trend break" in r


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Wrap original should_exit.

    trend_break exits were 14/15 losers (avg -38.8 EUR/trade) in the latest 102-trade
    quality sample -- even after a prior-cycle 2-bar close confirmation was added, the
    exit reason remained the single worst-performing category by a wide margin. This
    cycle tightens the confirmation further: both of the last 2 closes must be beyond
    EMA(50) by at least TREND_BREAK_ATR_BUFFER x ATR(14), not just a bare EMA cross.
    This adds hysteresis so a single-ATR-fraction wobble across EMA50 (common in a
    trend that is merely breathing, not reversing) no longer triggers an exit.
    hard_stop (77 trades, 46.8% win rate, ~breakeven avg_pnl) shows no clear exploitable
    pattern and is left untouched, as are the isolated single-trade STOP-LOSS / roster
    flatten entries which are too sparse (n=1 or forced-close events) to generalize from.
    """
    exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if exit_flag and _is_trend_break_reason(reason):
        try:
            close = df["Close"]
            high = df["High"]
            low = df["Low"]
            ema50 = _orig._ema(close, _orig.TREND_EMA)
            atr = _orig._atr(high, low, close, _orig.ATR_PERIOD)

            if len(close) >= 2:
                c1, c2 = close.iloc[-1], close.iloc[-2]
                e1, e2 = ema50.iloc[-1], ema50.iloc[-2]
                a1, a2 = atr.iloc[-1], atr.iloc[-2]
                direction = str(position.get("direction", "")).lower()

                buf1 = TREND_BREAK_ATR_BUFFER * a1 if pd.notna(a1) else 0.0
                buf2 = TREND_BREAK_ATR_BUFFER * a2 if pd.notna(a2) else 0.0

                if direction == "buy":
                    confirmed = (c1 < e1 - buf1) and (c2 < e2 - buf2)
                else:
                    confirmed = (c1 > e1 + buf1) and (c2 > e2 + buf2)

                if not confirmed:
                    return False, None
        except Exception:
            pass

    return exit_flag, reason
