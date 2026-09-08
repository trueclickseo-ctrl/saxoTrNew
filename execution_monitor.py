"""
execution_monitor.py
--------------------
Live-price execution window for ATOS entry orders.

Instead of firing a market order immediately at the signal bar's close,
this module polls Saxo's live bid/ask for up to `window_seconds` and
enters as soon as the predefined execution conditions are satisfied:

  Buy  — ask  <= signal_price * (1 + max_adverse_pct)
  Sell — bid  >= signal_price * (1 - max_adverse_pct)

On timeout the caller still places the order at the prevailing market
price (same behaviour as the old single-fetch path), so this is strictly
additive: it cannot make fill quality worse, only better or equal.

Every execution is recorded to data/execution_quality.db for empirical
comparison across window lengths (15 / 30 / 60 s).

Usage
-----
    import execution_monitor as em

    result = em.run_window(
        fetch_quote_fn = lambda: saxo_client.get_full_quote(uic, "Stock"),
        direction      = "Buy",
        signal_price   = 152.34,
        module         = "stocks",
        strategy       = "US Blend",
        symbol         = "AAPL",
        env            = "sim",
        window_seconds = 30.0,
    )
    # result is None only on a hard internal error
    # result.order_price  — mid at entry decision; use this to anchor stop/TP
    # result.timed_out    — True if no tick satisfied the condition in time

    # After placing the order and confirming fill:
    result.record_fill(fill_price=153.10, time_to_fill_s=1.8)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

_ROOT    = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_ROOT, "data", "execution_quality.db")

POLL_INTERVAL_S = 2.0   # seconds between live-quote polls


# ── DB helpers ───────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            exec_id              TEXT PRIMARY KEY,
            ts                   TEXT NOT NULL,
            module               TEXT NOT NULL,
            strategy             TEXT NOT NULL,
            symbol               TEXT NOT NULL,
            direction            TEXT NOT NULL,
            env                  TEXT NOT NULL,
            signal_price         REAL,
            signal_ts            TEXT,
            window_seconds       REAL,
            max_adverse_pct      REAL,
            elapsed_seconds      REAL,
            timed_out            INTEGER,
            entry_condition      TEXT,
            n_quotes             INTEGER,
            quotes_json          TEXT,
            bid_at_entry         REAL,
            ask_at_entry         REAL,
            mid_at_entry         REAL,
            spread_pct_at_entry  REAL,
            order_price          REAL,
            fill_price           REAL,
            slippage_vs_signal   REAL,
            slippage_vs_order    REAL,
            time_to_fill_s       REAL
        )
    """)
    con.commit()
    return con


def _db_insert(row: dict) -> None:
    try:
        con = _conn()
        cols   = ", ".join(row.keys())
        places = ", ".join("?" * len(row))
        con.execute(f"INSERT OR IGNORE INTO executions ({cols}) VALUES ({places})",
                    list(row.values()))
        con.commit()
        con.close()
    except Exception as exc:
        import sys
        print(f"  [exec_monitor] DB insert failed: {exc}", file=sys.stderr)


def _db_record_fill(exec_id: str, fill_price: float, time_to_fill_s: float) -> None:
    try:
        con = _conn()
        row = con.execute(
            "SELECT signal_price, order_price FROM executions WHERE exec_id=?",
            (exec_id,),
        ).fetchone()
        slippage_vs_signal = slippage_vs_order = None
        if row:
            sig_px, ord_px = row
            if sig_px:
                slippage_vs_signal = fill_price - sig_px
            if ord_px:
                slippage_vs_order = fill_price - ord_px
        con.execute(
            """UPDATE executions
               SET fill_price=?, slippage_vs_signal=?, slippage_vs_order=?,
                   time_to_fill_s=?
               WHERE exec_id=?""",
            (fill_price, slippage_vs_signal, slippage_vs_order,
             time_to_fill_s, exec_id),
        )
        con.commit()
        con.close()
    except Exception as exc:
        import sys
        print(f"  [exec_monitor] DB fill-update failed: {exc}", file=sys.stderr)


# ── Result ───────────────────────────────────────────────────────────────────

@dataclass
class WindowResult:
    exec_id:             str
    order_price:         float   # mid at entry decision — use for stop/TP anchoring
    bid_at_entry:        float
    ask_at_entry:        float
    spread_pct_at_entry: float
    elapsed_seconds:     float
    timed_out:           bool
    entry_condition:     str    # e.g. "price_ok @ 4.2s" or "timeout"
    n_quotes:            int

    def record_fill(self, fill_price: float, time_to_fill_s: float) -> None:
        """Call after the order is confirmed filled. Updates the DB row with
        fill_price, slippage_vs_signal, slippage_vs_order, time_to_fill_s."""
        if fill_price and fill_price > 0:
            _db_record_fill(self.exec_id, fill_price, time_to_fill_s)


# ── Core window ──────────────────────────────────────────────────────────────

def run_window(
    *,
    fetch_quote_fn:  Callable[[], dict | None],
    direction:       str,
    signal_price:    float,
    module:          str,
    strategy:        str,
    symbol:          str,
    env:             str,
    signal_ts:       str  = "",
    window_seconds:  float = 30.0,
    max_adverse_pct: float = 0.001,
    poll_interval:   float = POLL_INTERVAL_S,
) -> WindowResult | None:
    """Poll live bid/ask for up to window_seconds, entering when conditions
    are satisfied.  Returns a WindowResult (never None in normal operation).

    fetch_quote_fn must return {"bid": float, "ask": float, "mid": float,
    "spread_pct": float} or None on failure.  A None from the callback is
    treated as a missed tick — polling continues until the window expires.

    On timeout the last-seen quote (or signal_price as fallback) is used as
    order_price so the caller can still anchor stop/TP meaningfully.
    """
    exec_id  = uuid.uuid4().hex
    start_ts = datetime.now(timezone.utc)
    start_s  = time.monotonic()
    quotes:  list[dict] = []

    is_buy   = (direction == "Buy")
    # Adverse-move threshold: for a Buy the ask must not have run more than
    # max_adverse_pct above signal_price; for a Sell the bid must not have
    # dropped more than max_adverse_pct below signal_price.
    threshold = signal_price * (1 + max_adverse_pct if is_buy else -max_adverse_pct)

    entry_q   = None
    timed_out = True
    condition = "timeout"

    while True:
        elapsed = time.monotonic() - start_s

        q = None
        try:
            q = fetch_quote_fn()
        except Exception:
            pass

        if q:
            bid = float(q.get("bid") or 0)
            ask = float(q.get("ask") or 0)
            mid = float(q.get("mid") or 0)
            sp  = q.get("spread_pct")
            quotes.append({
                "t":  datetime.now(timezone.utc).isoformat(),
                "bid": bid, "ask": ask, "mid": mid,
                "sp": sp, "el": round(elapsed, 2),
            })
            cond_met = (ask <= threshold and ask > 0) if is_buy else (bid >= threshold and bid > 0)
            if cond_met:
                entry_q   = q
                timed_out = False
                condition = f"price_ok @ {elapsed:.1f}s"
                break

        if elapsed >= window_seconds:
            # Use the last observed quote; fall back to signal_price if none.
            if q and (q.get("mid") or 0) > 0:
                entry_q = q
            elif quotes:
                last   = quotes[-1]
                m      = last.get("mid") or signal_price
                entry_q = {"bid": m, "ask": m, "mid": m, "spread_pct": last.get("sp")}
            else:
                entry_q = {"bid": signal_price, "ask": signal_price,
                           "mid": signal_price, "spread_pct": None}
            break

        time.sleep(min(poll_interval, max(0.1, window_seconds - elapsed)))

    elapsed_total = time.monotonic() - start_s
    bid_e = float(entry_q.get("bid") or signal_price)
    ask_e = float(entry_q.get("ask") or signal_price)
    mid_e = float(entry_q.get("mid") or signal_price)
    sp_e  = entry_q.get("spread_pct")

    _db_insert({
        "exec_id":             exec_id,
        "ts":                  start_ts.isoformat(),
        "module":              module,
        "strategy":            strategy,
        "symbol":              symbol,
        "direction":           direction,
        "env":                 env,
        "signal_price":        signal_price,
        "signal_ts":           signal_ts,
        "window_seconds":      window_seconds,
        "max_adverse_pct":     max_adverse_pct,
        "elapsed_seconds":     round(elapsed_total, 2),
        "timed_out":           int(timed_out),
        "entry_condition":     condition,
        "n_quotes":            len(quotes),
        "quotes_json":         json.dumps(quotes),
        "bid_at_entry":        bid_e,
        "ask_at_entry":        ask_e,
        "mid_at_entry":        mid_e,
        "spread_pct_at_entry": float(sp_e) if sp_e is not None else None,
        "order_price":         mid_e,
        "fill_price":          None,
        "slippage_vs_signal":  None,
        "slippage_vs_order":   None,
        "time_to_fill_s":      None,
    })

    return WindowResult(
        exec_id             = exec_id,
        order_price         = mid_e,
        bid_at_entry        = bid_e,
        ask_at_entry        = ask_e,
        spread_pct_at_entry = float(sp_e) if sp_e is not None else 0.0,
        elapsed_seconds     = round(elapsed_total, 2),
        timed_out           = timed_out,
        entry_condition     = condition,
        n_quotes            = len(quotes),
    )
