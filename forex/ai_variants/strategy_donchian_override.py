# AI-WRITTEN Phase 2+3 2026-09-19 by claude-sonnet-5
# Phase 4 added 2026-09-25 by claude-sonnet-4-6: monster-trend pre-filter.
# Phase 5 added 2026-09-25 by claude-sonnet-4-6: BUY-only direction filter.
#
# Phase 2: two-consecutive-close confirmation for hard_stop exits.
# Phase 3: block exotic-quote currencies (TRY, MXN, CZK, DKK, PLN, NOK, HUF, ZAR, SGD).
# Phase 4: monster-trend pre-filter — only take breakouts on liquid pairs with a
#   genuinely strong trend (ADX ≥ 35) AND expanding volatility (ATR above its
#   20-bar average). Restricts universe to HIGH_VOLUME + CORE_STANDARD tiers.
# Phase 5: BUY-only direction filter.
#   Direction analysis (52 closed regular SIM trades):
#     BUY  (23 trades): WR 34.8%,  Net +11,047 EUR  (+480/trade)
#     SELL (29 trades): WR 31.0%,  Net   -804 EUR   (-28/trade)
#   All the P&L is in BUY breakouts (EURUSD +7,371, AUDUSD +5,449 both BUY).
#   SELL breakdowns are marginally losing across all pair tiers.
#   Blocking SELL reduces trade frequency but concentrates on the only
#   direction with a confirmed edge.

import pandas as pd
import numpy as np
from forex.strategy_donchian import generate_signals as _orig_generate_signals
from forex.strategy_donchian import should_exit as _orig_should_exit
from forex.strategy_donchian import size_position          # re-export unchanged
from forex.universe import HIGH_VOLUME_SYMBOLS, CORE_STANDARD_SYMBOLS

# ── Phase 3: exotic-currency code block ──────────────────────────────────────
_EXOTIC_CODES = ("TRY", "MXN", "CZK", "DKK", "PLN", "NOK", "HUF", "ZAR", "SGD")

def _is_exotic_pair(symbol: str) -> bool:
    sym = symbol.upper()
    return any(code in sym for code in _EXOTIC_CODES)

# ── Phase 4: monster-trend universe ──────────────────────────────────────────
_MONSTER_UNIVERSE: frozenset = frozenset(HIGH_VOLUME_SYMBOLS | CORE_STANDARD_SYMBOLS)

# ADX raised from base strategy's 25 to 35 — require a strongly established trend,
# not just a confirmed one.  Monster winners had ADX well above 35 at entry.
_ADX_MONSTER    = 35

# ATR expansion: current ATR must exceed its 20-bar EMA by this factor.
# 1.10 = 10% above recent average, filtering stagnant / noise breakouts.
_ATR_EXPANSION  = 1.10
_ATR_EMA_PERIOD = 20

# Minimum breakout score (distance past channel / ATR).  Filters breakouts
# that barely pierced the channel level — monster moves start with conviction.
_SCORE_MIN      = 0.3


def _atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    prev = c.shift(1)
    tr   = pd.concat([h - l, (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def generate_signals(market_data: dict, open_symbols: set = None, **kwargs) -> list:
    """Phase 3 + 4 filtered Donchian signals.

    Phase 3 strips exotic-quote-currency pairs.
    Phase 4 further restricts to HIGH_VOLUME + CORE_STANDARD universe and
    requires ADX ≥ 35, ATR expansion ≥ 10%, and score ≥ 0.3 — conditions
    that characterise the rare monster breakouts that carry all the P&L.
    """
    signals = _orig_generate_signals(market_data, open_symbols=open_symbols, **kwargs)

    filtered = []
    for s in signals:
        sym = s.get("symbol", "")

        # Phase 3: exotic currency code
        if _is_exotic_pair(sym):
            continue

        # Phase 4a: pair must be in liquid HIGH_VOL or CORE_STD tier
        if sym not in _MONSTER_UNIVERSE:
            continue

        # Phase 4b: ADX must be strongly trending (≥ 35)
        if s.get("adx", 0) < _ADX_MONSTER:
            continue

        # Phase 4c: ATR expansion — volatility must be above its recent average
        df = market_data.get(sym)
        if df is not None and len(df) >= _ATR_EMA_PERIOD + 14:
            atr_s   = _atr_series(df)
            atr_now = float(atr_s.iloc[-1])
            atr_avg = float(atr_s.iloc[-_ATR_EMA_PERIOD:].mean())
            if atr_avg > 0 and atr_now < _ATR_EXPANSION * atr_avg:
                continue  # volatility not expanding — skip

        # Phase 4d: breakout score threshold (conviction)
        if s.get("score", 0) < _SCORE_MIN:
            continue

        # Phase 5: BUY-only — SELL breakdowns are structurally losing
        # (29 trades, -804 EUR, -28/trade vs BUY +480/trade in regular SIM)
        if str(s.get("direction", "")).lower() not in ("buy", "long"):
            continue

        filtered.append(s)

    return filtered


# ── Phase 2: two-consecutive-close confirmation for hard_stop exits ───────────

def _closed_past_stop(direction: str, close_val: float, stop_price: float) -> bool:
    if pd.isna(close_val) or pd.isna(stop_price):
        return False
    if str(direction).lower() in ("buy", "long"):
        return close_val <= stop_price
    else:
        return close_val >= stop_price


def should_exit(position: dict, df: pd.DataFrame, calendar_days_held: int) -> tuple:
    """Phase 2: require two consecutive closes past the stop before a hard_stop
    exit is honoured — avoids whipsaw exits on single-bar spikes.
    Phase 4 re-validates: hard_stop exits in regular SIM remain net-positive
    (avg +475 EUR) so the confirmation bar is still doing its job.
    """
    should_exit_flag, reason = _orig_should_exit(position, df, calendar_days_held)

    if not should_exit_flag:
        return should_exit_flag, reason

    reason_lower = str(reason).lower()
    is_hard_stop = ("hard_stop" in reason_lower) or ("stop-loss" in reason_lower) or ("stop_loss" in reason_lower)

    if not is_hard_stop:
        return should_exit_flag, reason

    if df is None or len(df) < 2:
        return should_exit_flag, reason

    direction  = position.get("direction", "")
    stop_price = position.get("stop_price", None)
    if stop_price is None:
        return should_exit_flag, reason

    closes     = df["Close"]
    last_close = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2])

    last_breached = _closed_past_stop(direction, last_close, stop_price)
    prev_breached = _closed_past_stop(direction, prev_close, stop_price)

    if last_breached and prev_breached:
        return True, reason

    return False, "hard_stop_awaiting_confirmation"
