"""
avanza_state.py
---------------
SQLite-backed trade ledger + JSON status file for the Avanza sleeve.

Files:
    data/avanza_trades.db    — closed + open trade history
    data/avanza_status.json  — last-run summary for the dashboard
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH     = os.path.join(_ROOT, "data", "avanza_trades.db")
STATUS_FILE = os.path.join(_ROOT, "data", "avanza_status.json")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT NOT NULL,
    order_book_id TEXT NOT NULL,
    side          TEXT NOT NULL,        -- BUY / SELL
    qty           INTEGER NOT NULL,
    price         REAL,
    price_currency TEXT DEFAULT 'USD',
    value_sek     REAL,
    order_id      TEXT,
    status        TEXT DEFAULT 'OPEN',  -- OPEN / FILLED / CANCELLED
    strategy      TEXT DEFAULT 'US Blend',
    entry_date    TEXT,
    exit_date     TEXT,
    entry_price   REAL,
    exit_price    REAL,
    pnl_sek       REAL,
    note          TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);
"""


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(_SCHEMA)
    con.commit()
    return con


# ── Write ──────────────────────────────────────────────────────────────────────

def record_order(ticker: str, order_book_id: str, side: str, qty: int,
                 price: float, order_id: str | None,
                 currency: str = "USD", value_sek: float = 0.0,
                 note: str = "") -> int:
    """Insert a new order row. Returns the row id."""
    today = datetime.now().isoformat()
    with _conn() as con:
        cur = con.execute(
            """INSERT INTO trades
               (ticker, order_book_id, side, qty, price, price_currency,
                value_sek, order_id, status, entry_date, entry_price, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, order_book_id, side, qty, price, currency,
             value_sek, order_id, "OPEN", today, price, note)
        )
        return cur.lastrowid


def mark_filled(order_id: str, fill_price: float | None = None,
                value_sek: float | None = None) -> None:
    """Mark an order as FILLED (after confirming via Avanza)."""
    with _conn() as con:
        if fill_price is not None:
            con.execute(
                "UPDATE trades SET status='FILLED', exit_price=? WHERE order_id=?",
                (fill_price, order_id)
            )
        else:
            con.execute("UPDATE trades SET status='FILLED' WHERE order_id=?", (order_id,))
        if value_sek is not None:
            con.execute("UPDATE trades SET value_sek=? WHERE order_id=?", (value_sek, order_id))


def record_close(ticker: str, order_book_id: str, qty: int,
                 exit_price: float, order_id: str | None,
                 currency: str = "USD", pnl_sek: float = 0.0,
                 note: str = "") -> None:
    """Record a sell / close order in the ledger."""
    today = datetime.now().isoformat()
    with _conn() as con:
        con.execute(
            """INSERT INTO trades
               (ticker, order_book_id, side, qty, price, price_currency,
                order_id, status, exit_date, exit_price, pnl_sek, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, order_book_id, "SELL", qty, exit_price, currency,
             order_id, "OPEN", today, exit_price, pnl_sek, note)
        )


# ── Read ───────────────────────────────────────────────────────────────────────

def get_open_buys() -> list[dict]:
    """Return all BUY orders with status=OPEN or FILLED (= positions held)."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades WHERE side='BUY' AND status IN ('OPEN','FILLED') "
            "ORDER BY entry_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_trades(n: int = 20) -> list[dict]:
    """Return the N most recent trades."""
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_today_pnl_sek() -> float:
    """Realized P&L for today (SELL rows with status=FILLED created today)."""
    today = datetime.now().date().isoformat()
    with _conn() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(pnl_sek),0) FROM trades "
            "WHERE side='SELL' AND status='FILLED' AND substr(created_at,1,10)=?",
            (today,)
        ).fetchone()
    return float(row[0])


# ── Status file (for dashboard) ────────────────────────────────────────────────

def write_status(payload: dict) -> None:
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, STATUS_FILE)


def read_status() -> dict:
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
