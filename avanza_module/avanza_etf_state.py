"""
avanza_etf_state.py
-------------------
SQLite ledger for Avanza ETF positions. Separate DB from avanza_trades.db
so stocks and ETFs never interfere.

DB: data/avanza_etf_trades.db
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_ROOT, "data", "avanza_etf_trades.db")
_STATUS_FILE = os.path.join(_ROOT, "data", "avanza_etf_status.json")


def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("""
        CREATE TABLE IF NOT EXISTS etf_trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker           TEXT NOT NULL,
            order_book_id    TEXT NOT NULL,
            side             TEXT NOT NULL,
            qty              REAL NOT NULL,
            entry_price      REAL,
            stop_price       REAL,
            trailing_high    REAL,
            stop_order_id    TEXT,
            order_id         TEXT,
            status           TEXT DEFAULT 'OPEN',
            value_sek        REAL,
            pnl_sek          REAL,
            opened_at        TEXT,
            closed_at        TEXT,
            close_price      REAL,
            momentum_score   REAL,
            rank_at_entry    INTEGER
        )
    """)
    con.commit()
    return con


def record_order(ticker: str, ob_id: str, qty: float, price: float,
                 order_id: str, value_sek: float = 0,
                 momentum_score: float | None = None,
                 rank_at_entry: int | None = None) -> int:
    con  = _conn()
    cur  = con.execute(
        """INSERT INTO etf_trades
           (ticker, order_book_id, side, qty, entry_price, order_id,
            status, value_sek, opened_at, momentum_score, rank_at_entry)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (ticker, ob_id, "BUY", qty, price, order_id,
         "PENDING", value_sek, datetime.now().isoformat(),
         momentum_score, rank_at_entry),
    )
    trade_id = cur.lastrowid
    con.commit()
    con.close()
    return trade_id


def mark_filled(order_id: str, fill_price: float) -> None:
    con = _conn()
    con.execute(
        "UPDATE etf_trades SET status='OPEN', entry_price=?, trailing_high=? WHERE order_id=?",
        (fill_price, fill_price, order_id),
    )
    con.commit()
    con.close()


def mark_cancelled(order_id: str) -> None:
    con = _conn()
    con.execute("UPDATE etf_trades SET status='CANCELLED' WHERE order_id=?", (order_id,))
    con.commit()
    con.close()


def update_stop(trade_id: int, stop_order_id: str | None,
                stop_price: float, trailing_high: float) -> None:
    con = _conn()
    con.execute(
        """UPDATE etf_trades SET stop_order_id=?, stop_price=?, trailing_high=?
           WHERE id=?""",
        (stop_order_id, stop_price, trailing_high, trade_id),
    )
    con.commit()
    con.close()


def record_close(ticker: str, ob_id: str, qty: float, close_price: float,
                 order_id: str, pnl_sek: float = 0) -> None:
    con = _conn()
    con.execute(
        """INSERT INTO etf_trades
           (ticker, order_book_id, side, qty, close_price, order_id,
            status, pnl_sek, opened_at, closed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ticker, ob_id, "SELL", qty, close_price, order_id,
         "CLOSED", pnl_sek, datetime.now().isoformat(), datetime.now().isoformat()),
    )
    # Mark matching open SELL as closed
    con.execute(
        """UPDATE etf_trades SET status='CLOSED', close_price=?, closed_at=?, pnl_sek=?
           WHERE ticker=? AND side='BUY' AND status='OPEN'""",
        (close_price, datetime.now().isoformat(), pnl_sek, ticker),
    )
    con.commit()
    con.close()


def get_open_positions() -> list[dict]:
    con  = _conn()
    rows = con.execute(
        "SELECT * FROM etf_trades WHERE side='BUY' AND status='OPEN'"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def get_held_tickers() -> set[str]:
    return {p["ticker"].upper() for p in get_open_positions()}


def get_today_pnl_sek() -> float:
    con = _conn()
    today = datetime.now().strftime("%Y-%m-%d")
    row = con.execute(
        "SELECT COALESCE(SUM(pnl_sek),0) FROM etf_trades WHERE closed_at LIKE ?",
        (f"{today}%",),
    ).fetchone()
    con.close()
    return float(row[0]) if row else 0.0


def write_status(data: dict) -> None:
    os.makedirs(os.path.dirname(_STATUS_FILE), exist_ok=True)
    with open(_STATUS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def read_status() -> dict:
    if os.path.exists(_STATUS_FILE):
        try:
            with open(_STATUS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}
