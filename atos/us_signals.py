"""
atos/us_signals.py
-------------------
Adapter layer: wraps the 4 usa_strategy strategies (SMAStrategy, RSIStrategy,
MomentumStrategy, EnsembleStrategy) for use in atos_runner.py.

Strategies and their strategy-column names in the trades DB:
  SMAStrategy      -> "US SMA Crossover"
  RSIStrategy      -> "US RSI Reversal"
  MomentumStrategy -> "US Momentum"
  EnsembleStrategy -> "US Ensemble"

All 4 run SIM-only. Core strategies (US Blend, US Reversion) are untouched.

Entry:
  - BUY signal from strategy on today's OHLCV bar
  - At most MAX_POSITIONS_PER_STRATEGY open at once per strategy
  - 1 position per (ticker, strategy) at a time

Stop:
  - 2.0 * ATR(14), floor at HARD_STOP_PCT below entry

Exit:
  - SELL signal from the same strategy that entered, OR
  - Hard stop hit (stop_price from DB), OR
  - MAX_HOLD_DAYS time exit

Never imports forex/runner, saxo_*, atos_runner, or any order/position module.
Callers (atos_runner.py) own all order placement.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from datetime import date, datetime, timezone
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────────────────

STRATEGY_NAMES: dict[str, str] = {
    "SMAStrategy":      "US SMA Crossover",
    "RSIStrategy":      "US RSI Reversal",
    "MomentumStrategy": "US Momentum",
    "EnsembleStrategy": "US Ensemble",
}

# Reverse map: DB strategy column -> package class name
_DB_TO_PKG: dict[str, str] = {v: k for k, v in STRATEGY_NAMES.items()}

ALL_SIGNAL_STRATEGY_NAMES: list[str] = list(STRATEGY_NAMES.values())

MAX_POSITIONS_PER_STRATEGY: int = 2     # max open positions per strategy
MAX_HOLD_DAYS: int                = 30  # time-based exit
HARD_STOP_PCT: float              = 0.06  # 6% max loss below entry
ATR_STOP_MULT: float              = 2.0   # ATR multiplier for stop


# ── DataFrame preparation ──────────────────────────────────────────────────────

def _prep_df(feat_df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert a feat_data DataFrame (add_all() output with yfinance columns)
    into the shape usa_strategy expects: lowercase 'price'/'close'/'volume'
    columns, a 'timestamp' column.
    """
    df = feat_df.copy()
    df.reset_index(drop=True, inplace=True)

    # Normalise price column
    if "price" not in df.columns:
        for src in ("close", "Close", "Adj Close"):
            if src in df.columns:
                df["price"] = df[src].astype(float)
                break

    # Normalise close
    if "close" not in df.columns and "Close" in df.columns:
        df["close"] = df["Close"].astype(float)

    # Normalise volume
    if "volume" not in df.columns and "Volume" in df.columns:
        df["volume"] = df["Volume"].astype(float)

    # Ensure timestamp column
    if "timestamp" not in df.columns:
        df["timestamp"] = pd.date_range(
            end=datetime.now(timezone.utc), periods=len(df), freq="D"
        )

    return df


# ── Signal generation ──────────────────────────────────────────────────────────

def _run_one(strategy_cls_name: str, ticker: str, df: pd.DataFrame):
    """Return the SignalResult for one strategy on this ticker/df. Never raises."""
    try:
        from atos.usa_strategy import (
            SMAStrategy, RSIStrategy, MomentumStrategy, EnsembleStrategy,
            StrategyConfig,
        )
        _map = {
            "SMAStrategy":      SMAStrategy,
            "RSIStrategy":      RSIStrategy,
            "MomentumStrategy": MomentumStrategy,
            "EnsembleStrategy": EnsembleStrategy,
        }
        cls = _map[strategy_cls_name]
        cfg = StrategyConfig()
        if strategy_cls_name == "SMAStrategy":
            result = cls(cfg).generate(ticker, df)
        else:
            result = cls(cfg).generate(ticker, df)
        return result
    except Exception:
        return None


def get_entry_signals(ticker: str, feat_df: pd.DataFrame) -> list[dict]:
    """
    Return a list of BUY signals for this ticker from all 4 strategies.
    Each entry: {strategy_name, confidence, reason, pkg_cls}.
    """
    try:
        df = _prep_df(feat_df)
    except Exception:
        return []

    results = []
    for pkg_cls, db_name in STRATEGY_NAMES.items():
        r = _run_one(pkg_cls, ticker, df)
        if r is None:
            continue
        if r.signal == "BUY":
            results.append({
                "strategy_name": db_name,
                "confidence":    r.confidence,
                "reason":        r.reason,
                "pkg_cls":       pkg_cls,
            })
    return results


def should_exit(trade: dict, feat_df: pd.DataFrame, current_price: float) -> tuple[bool, str]:
    """
    Check if an open US Signals trade should be exited.
    Checks (in order): stop hit, time limit, SELL signal from original strategy.
    Returns (exit, reason).
    """
    # 1. Hard stop
    stop = float(trade.get("stop_price") or 0)
    if stop > 0 and current_price <= stop:
        return True, f"stop hit @ {current_price:.2f} (stop {stop:.2f})"

    # 2. Time limit
    entry_date_str = trade.get("entry_date", "")
    if entry_date_str:
        try:
            held = (date.today() - date.fromisoformat(entry_date_str[:10])).days
            if held >= MAX_HOLD_DAYS:
                return True, f"time exit: {held}d >= {MAX_HOLD_DAYS}d max hold"
        except Exception:
            pass

    # 3. SELL signal from original strategy
    db_strategy = trade.get("strategy", "")
    pkg_cls = _DB_TO_PKG.get(db_strategy)
    if pkg_cls:
        try:
            df = _prep_df(feat_df)
            r = _run_one(pkg_cls, trade.get("ticker", ""), df)
            if r is not None and r.signal == "SELL":
                return True, f"{db_strategy} SELL: {r.reason[:80]}"
        except Exception:
            pass

    return False, ""


# ── Stop loss calculation ──────────────────────────────────────────────────────

def compute_stop(feat_df: pd.DataFrame, entry_price: float) -> float:
    """
    ATR(14) * ATR_STOP_MULT below entry, floored at HARD_STOP_PCT.
    Never raises.
    """
    try:
        df = feat_df
        atr_col = None
        for col in ("atr", "ATR"):
            if col in df.columns:
                atr_col = col
                break

        if atr_col:
            atr = float(df[atr_col].dropna().iloc[-1])
            atr_stop = entry_price - ATR_STOP_MULT * atr
        else:
            atr_stop = 0.0

        hard_stop = entry_price * (1 - HARD_STOP_PCT)
        stop = max(atr_stop, hard_stop)
        return round(stop, 4)
    except Exception:
        return round(entry_price * (1 - HARD_STOP_PCT), 4)


# ── Per-strategy DB stats ─────────────────────────────────────────────────────

def compute_stats(closed_trades: list[dict]) -> dict[str, dict]:
    """
    Compute WR and PF for each of the 4 US Signals strategy names.
    Input: list of closed trade dicts (strategy, pnl_sek, was_profitable).
    Output: {strategy_name: {wins, losses, wr_pct, pf, total_pnl_sek}}.
    """
    out: dict[str, dict] = {
        name: {"wins": 0, "losses": 0, "gross_win": 0.0, "gross_loss": 0.0}
        for name in ALL_SIGNAL_STRATEGY_NAMES
    }
    for t in closed_trades:
        s = t.get("strategy", "")
        if s not in out:
            continue
        pnl = float(t.get("pnl_sek") or 0)
        if pnl > 0:
            out[s]["wins"] += 1
            out[s]["gross_win"] += pnl
        else:
            out[s]["losses"] += 1
            out[s]["gross_loss"] += abs(pnl)

    result: dict[str, dict] = {}
    for name, d in out.items():
        n = d["wins"] + d["losses"]
        wr = (d["wins"] / n * 100) if n > 0 else None
        pf = (d["gross_win"] / d["gross_loss"]) if d["gross_loss"] > 0 else None
        result[name] = {
            "wins":          d["wins"],
            "losses":        d["losses"],
            "total_trades":  n,
            "wr_pct":        round(wr, 1) if wr is not None else None,
            "pf":            round(pf, 2) if pf is not None else None,
            "gross_win_sek": round(d["gross_win"], 0),
            "gross_loss_sek": round(d["gross_loss"], 0),
        }
    return result
