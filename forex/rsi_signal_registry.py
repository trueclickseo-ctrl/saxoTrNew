"""
forex/rsi_signal_registry.py -- record EVERY RSI(2) trigger (SIM + LIVE),
including the ones the live threshold (RSI_OVERSOLD = 10) currently
rejects, so "what's the best RSI threshold after costs" can be answered
with real forward data instead of a curve-fit backtest.

Two passes, both called once per RSI scan from forex/runner.py:

  observe(account_env, market_data, fired_syms, taken_syms)
    for every pair with RSI(2) in the STUDY band (<= STUDY_MAX_LONG /
    >= STUDY_MIN_SHORT) and the trend filter satisfied, append one row to
    data/rsi_signal_registry.jsonl: the rsi2 value, price, atr, the
    1.5xATR stop, whether it fired at the live threshold, whether it was
    actually entered.

  resolve(account_env, market_data)
    for each still-unresolved row within TIME_STOP_DAYS, walk the daily
    bars from entry and record the FIRST of: 1.5xATR stop hit / 2R TP hit
    / RSI(2) recovery / 12-day time stop -> {outcome, exit_price,
    r_multiple, days_held}.

The live entry threshold is NOT changed -- runner still only acts on
RSI(2) <= 10. This module only observes. report_rsi_thresholds.py buckets
the resolved rows.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone

import numpy as np

import forex.strategy_rsi as srsi

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
REGISTRY = os.path.join(_DATA_DIR, "rsi_signal_registry.jsonl")

# Study band -- wider than the live RSI_OVERSOLD/OVERBOUGHT so rows exist
# to compare thresholds against.
STUDY_MAX_LONG   = 15.0
STUDY_MIN_SHORT  = 85.0


def _append(row: dict) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(REGISTRY, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception:
        pass  # observation logging must never break a run


def _load_all() -> list[dict]:
    if not os.path.exists(REGISTRY):
        return []
    out = []
    for ln in open(REGISTRY, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _rewrite(rows: list[dict]) -> None:
    try:
        tmp = REGISTRY + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        os.replace(tmp, REGISTRY)
    except Exception:
        pass


def _row_key(r: dict) -> str:
    return f"{r.get('account_env')}|{r.get('symbol')}|{r.get('bar_date')}|{r.get('direction')}"


def observe(account_env: str, market_data: dict,
            fired_syms: set | None = None, taken_syms: set | None = None) -> int:
    """Log every study-band RSI(2) trigger this scan. Idempotent per
    (account, symbol, bar_date, direction) -- re-running the same day's
    scan won't duplicate rows."""
    fired_syms = set(fired_syms or ())
    taken_syms = set(taken_syms or ())
    existing = {_row_key(r) for r in _load_all()}
    n = 0
    for sym, df in (market_data or {}).items():
        if df is None or len(df) < srsi.MIN_BARS:
            continue
        c = df["Close"].astype(float)
        rsi_s = srsi._rsi(c)
        ema_s = srsi._ema(c, srsi.TREND_EMA)
        atr_s = srsi._atr(df["High"].astype(float), df["Low"].astype(float), c)
        cur_rsi, cur_ema = float(rsi_s.iloc[-1]), float(ema_s.iloc[-1])
        cur_close, cur_atr = float(c.iloc[-1]), float(atr_s.iloc[-1])
        if any(np.isnan(x) for x in (cur_rsi, cur_ema, cur_atr)) or cur_atr <= 0:
            continue

        direction = None
        if cur_close > cur_ema and cur_rsi <= STUDY_MAX_LONG:
            direction = "Buy"
            stop = cur_close - srsi.ATR_STOP_MULT * cur_atr
        elif cur_close < cur_ema and cur_rsi >= STUDY_MIN_SHORT:
            direction = "Sell"
            stop = cur_close + srsi.ATR_STOP_MULT * cur_atr
        if direction is None:
            continue

        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "bar_date": date.today().isoformat(),
            "account_env": account_env,
            "symbol": sym,
            "direction": direction,
            "rsi2": round(cur_rsi, 2),
            "close": cur_close,
            "atr": round(cur_atr, 6),
            "stop": round(stop, 6),
            "r_price": round(srsi.ATR_STOP_MULT * cur_atr, 6),   # 1 R in price
            "ema200_dist_pct": round((cur_close - cur_ema) / cur_ema * 100, 3),
            "fired_at_live_threshold": sym in fired_syms or cur_rsi <= srsi.RSI_OVERSOLD or cur_rsi >= srsi.RSI_OVERBOUGHT,
            "entered": sym in taken_syms,
            "resolved": None,
        }
        if _row_key(row) in existing:
            continue
        _append(row)
        n += 1
    return n


def resolve(account_env: str, market_data: dict) -> int:
    """Fill in `resolved` for unresolved rows using the daily bars now in
    hand. First-touch wins: stop / 2R TP / RSI recovery / 12-day time."""
    rows = _load_all()
    changed = 0
    today = date.today()
    for r in rows:
        if r.get("resolved") or r.get("account_env") != account_env:
            continue
        try:
            entry_d = date.fromisoformat(r["bar_date"])
        except Exception:
            continue
        df = (market_data or {}).get(r["symbol"])
        if df is None or len(df) < 3:
            continue

        is_long = r["direction"] == "Buy"
        entry, R = float(r["close"]), float(r["r_price"])
        stop = float(r["stop"])
        tp   = entry + 2 * R if is_long else entry - 2 * R
        c = df["Close"].astype(float)
        h = df["High"].astype(float)
        l = df["Low"].astype(float)
        rsi_s = srsi._rsi(c)

        # daily bars have no date index here; walk the last (today-entry)+2 bars
        days_elapsed = (today - entry_d).days
        look = min(len(df), days_elapsed + 2)
        outcome = exit_px = None
        for i in range(-look, 0):
            bar_lo, bar_hi = float(l.iloc[i]), float(h.iloc[i])
            bar_rsi = float(rsi_s.iloc[i])
            if is_long:
                if bar_lo <= stop:
                    outcome, exit_px = "stop", stop; break
                if bar_hi >= tp:
                    outcome, exit_px = "tp_2r", tp; break
                if not np.isnan(bar_rsi) and bar_rsi >= srsi.RSI_EXIT_LONG:
                    outcome, exit_px = "rsi_recovery", float(c.iloc[i]); break
            else:
                if bar_hi >= stop:
                    outcome, exit_px = "stop", stop; break
                if bar_lo <= tp:
                    outcome, exit_px = "tp_2r", tp; break
                if not np.isnan(bar_rsi) and bar_rsi <= srsi.RSI_EXIT_SHORT:
                    outcome, exit_px = "rsi_recovery", float(c.iloc[i]); break
        if outcome is None and days_elapsed >= srsi.TIME_STOP_DAYS:
            outcome, exit_px = "time_stop", float(c.iloc[-1])
        if outcome is None:
            continue  # still open

        r_mult = ((exit_px - entry) / R if is_long else (entry - exit_px) / R) if R > 0 else 0.0
        r["resolved"] = {
            "resolved_ts": datetime.now(timezone.utc).isoformat(),
            "outcome": outcome,
            "exit_price": round(exit_px, 6),
            "r_multiple": round(r_mult, 3),
            "days_held": days_elapsed,
        }
        changed += 1
    if changed:
        _rewrite(rows)
    return changed
