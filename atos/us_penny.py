"""
atos/us_penny.py
-----------------
US Penny Stock momentum breakout strategy.

STATUS: SIM-ONLY. Never gets LIVE entries — these are sub-$2 stocks with
extreme volatility and wide spreads. Observe signal quality before any
capital commitment.

LOGIC:
  Penny stocks are NOT dip-buying candidates — when they fall they often
  keep falling. The edge is momentum breakout: buying when a beaten-down
  name starts moving up with volume confirmation.

ENTRY (all conditions must hold):
  1. Close > 20-day Donchian high (prior 20 bars) — price breaks out
  2. Volume today >= VOL_MULT (2.0x) × 20-day avg — volume confirms move
  3. Close > SMA10 — short-term uptrend intact

EXIT (first condition hit):
  A. Price reaches +TARGET_PCT (25%) above entry — profit target
  B. Hard stop: price drops STOP_PCT (12%) below entry (wide — penny volatility)
  C. Max hold: MAX_HOLD_DAYS (15) trading days

BACKTEST (2023-2026, 10 passing tickers):
  AKBA 75% WR +14.9% avg  |  UWMC 75% WR +7.5%  |  HRTX/CHRS 50% WR +6.5%
  Universe WR 42%, avg ret +3.6% (ADD tickers only)

UNIVERSE: PENNY_TICKERS (10 names) — curated from 29-ticker screener pass.
  All must be currently under $2 with > 500k avg daily volume.

This module is PURE (no I/O, no orders):
  - scan(feat_data, tickers)          -> candidate list (entry signals)
  - should_exit(trade, price, high)   -> (exit: bool, reason: str)
The runner executes orders and manages state.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# ── Strategy parameters ────────────────────────────────────────────────────────
BREAKOUT_DAYS  = 20     # close must exceed prior 20-day high
VOL_MULT       = 2.0    # volume must be >= 2x 20-day average
SMA_PERIOD     = 10     # must be above SMA10
TARGET_PCT     = 0.25   # +25% profit target
STOP_PCT       = 0.12   # -12% hard stop
MAX_HOLD_DAYS  = 15     # 15-day time stop
MAX_POSITIONS  = 5      # max concurrent penny positions
MAX_PRICE      = 2.00   # hard gate: skip if price has risen above $2
# ─────────────────────────────────────────────────────────────────────────────


def _sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def scan(feat_data: dict, tickers: list) -> list:
    """Scan PENNY_TICKERS for momentum breakout entry signals.

    Returns a ranked list of dicts (highest volume ratio first):
      {ticker, price, sma10, don_high, vol_ratio, pct_above_don, score}

    Caller decides how many slots are open before acting on signals.
    """
    candidates = []

    for ticker in tickers:
        df = feat_data.get(ticker)
        if df is None or "Close" not in df or "Volume" not in df:
            continue

        close  = df["Close"].dropna()
        volume = df["Volume"].dropna()

        if len(close) < BREAKOUT_DAYS + SMA_PERIOD + 5:
            continue

        price      = float(close.iloc[-1])
        sma10      = float(_sma(close, SMA_PERIOD).iloc[-1])
        avg_vol    = float(volume.rolling(20).mean().iloc[-1])
        vol_today  = float(volume.iloc[-1])

        # 20-day Donchian high (prior bars only — exclude today)
        don_high   = float(close.iloc[-(BREAKOUT_DAYS + 1):-1].max())

        if pd.isna(sma10) or pd.isna(avg_vol) or avg_vol <= 0:
            continue

        # Hard price gate — if it already ran past $2 skip it
        if price > MAX_PRICE * 1.10:
            continue

        vol_ratio      = vol_today / avg_vol
        above_don      = price > don_high
        vol_confirmed  = vol_ratio >= VOL_MULT
        above_sma      = price > sma10

        if not (above_don and vol_confirmed and above_sma):
            continue

        pct_above_don = (price - don_high) / don_high * 100
        score         = vol_ratio * pct_above_don   # bigger move + bigger volume = stronger

        candidates.append({
            "ticker":        ticker,
            "price":         round(price, 4),
            "sma10":         round(sma10, 4),
            "don_high":      round(don_high, 4),
            "vol_ratio":     round(vol_ratio, 2),
            "pct_above_don": round(pct_above_don, 2),
            "score":         round(score, 4),
        })

    return sorted(candidates, key=lambda x: x["score"], reverse=True)


def should_exit(
    trade: dict,
    current_price: float,
    current_high: float | None = None,
) -> tuple[bool, str]:
    """Check exit conditions for an open penny position.

    Args:
        trade:          trade dict with at least 'entry_price' and 'days_held'
        current_price:  latest price (close or intraday)
        current_high:   session high (used to check if target was touched intraday)

    Returns:
        (should_exit, reason)  reason in {"target", "stop", "time", ""}
    """
    entry      = trade.get("entry_price", 0)
    days_held  = trade.get("days_held", 0)

    if entry <= 0:
        return False, ""

    target_price = entry * (1 + TARGET_PCT)
    stop_price   = entry * (1 - STOP_PCT)

    # Profit target — check intraday high if provided
    check_high = current_high if current_high is not None else current_price
    if check_high >= target_price:
        return True, "target"

    # Hard stop
    if current_price <= stop_price:
        return True, "stop"

    # Time stop
    if days_held >= MAX_HOLD_DAYS:
        return True, "time"

    return False, ""
