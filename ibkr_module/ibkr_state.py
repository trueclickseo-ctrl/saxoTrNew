"""
ibkr_state.py
-------------
SQLite ledger for the IBKR stocks sleeve.
Database: data/ibkr_stocks.db

No Saxo imports. No Avanza imports.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "IBKR_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "ibkr_stocks.db"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      TEXT    NOT NULL,
    symbol        TEXT    NOT NULL,
    side          TEXT    NOT NULL,
    qty           REAL    NOT NULL,
    limit_price   REAL,
    fill_price    REAL,
    stop_price    REAL,
    stop_order_id TEXT,
    trailing_high REAL,
    status        TEXT    NOT NULL DEFAULT 'PENDING',
    created_at    TEXT    NOT NULL,
    filled_at     TEXT,
    strategy      TEXT    NOT NULL DEFAULT 'blend'
);
"""


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema — safe to re-run."""
    try:
        con.execute("ALTER TABLE trades ADD COLUMN strategy TEXT DEFAULT 'blend'")
    except sqlite3.OperationalError:
        pass  # column already exists


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        con.execute(_SCHEMA)
        _migrate(con)
        con.commit()
        yield con
        con.commit()
    finally:
        con.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Write ─────────────────────────────────────────────────────────────────────

def record_order(order_id: str, symbol: str, side: str,
                 qty: int, limit_price: float | None = None,
                 strategy: str = "blend") -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO trades "
            "(order_id, symbol, side, qty, limit_price, status, created_at, strategy) "
            "VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)",
            (str(order_id), symbol.upper(), side.upper(), qty, limit_price, _now(), strategy),
        )


def mark_filled(order_id: str, fill_price: float, side: str | None = None) -> None:
    with _conn() as con:
        if side and side.upper() == "BUY":
            con.execute(
                "UPDATE trades SET fill_price=?, trailing_high=?, status='FILLED', filled_at=? "
                "WHERE order_id=?",
                (fill_price, fill_price, _now(), str(order_id)),
            )
        else:
            con.execute(
                "UPDATE trades SET fill_price=?, status='FILLED', filled_at=? WHERE order_id=?",
                (fill_price, _now(), str(order_id)),
            )


def mark_cancelled(order_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE trades SET status='CANCELLED' WHERE order_id=?", (str(order_id),)
        )


def update_stop(symbol: str, stop_price: float,
                stop_order_id: str, trailing_high: float,
                strategy: str | None = None) -> None:
    """Update stop for a filled BUY position.

    strategy: when supplied, scopes the update to that strategy only (avoids
    clobbering a different strategy's stop_order_id if the same ticker is held
    by multiple strategies simultaneously, e.g. 'US SMA Crossover' + 'US RSI Reversal').
    """
    sql = ("UPDATE trades SET stop_price=?, stop_order_id=?, trailing_high=? "
           "WHERE symbol=? AND status='FILLED' AND side='BUY'")
    params: list = [stop_price, str(stop_order_id), trailing_high, symbol.upper()]
    if strategy:
        sql += " AND strategy=?"
        params.append(strategy)
    with _conn() as con:
        con.execute(sql, params)


# ── Read ──────────────────────────────────────────────────────────────────────

def get_open_positions(strategy: str | None = None) -> list[dict]:
    """Return rows where side=BUY and status=FILLED. Optionally filter by strategy."""
    with _conn() as con:
        if strategy:
            rows = con.execute(
                "SELECT * FROM trades WHERE side='BUY' AND status='FILLED' AND strategy=? "
                "ORDER BY filled_at",
                (strategy,),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM trades WHERE side='BUY' AND status='FILLED' ORDER BY filled_at"
            ).fetchall()
    return [dict(r) for r in rows]


def count_open(strategy: str | None = None) -> int:
    """Count currently held positions (optionally filtered by strategy)."""
    return len(get_open_positions(strategy))


def get_all_trades(limit: int = 200) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def is_symbol_held(symbol: str) -> bool:
    with _conn() as con:
        row = con.execute(
            "SELECT 1 FROM trades WHERE symbol=? AND side='BUY' AND status='FILLED'",
            (symbol.upper(),),
        ).fetchone()
    return row is not None


def get_today_pnl_usd() -> float:
    """Sum of (fill_price - entry) * qty for all SELL trades filled today."""
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as con:
        rows = con.execute(
            "SELECT t_sell.fill_price AS sell_price, t_buy.fill_price AS buy_price, t_sell.qty "
            "FROM trades t_sell "
            "JOIN trades t_buy ON t_sell.symbol = t_buy.symbol "
            "WHERE t_sell.side='SELL' AND t_sell.status='FILLED' "
            "AND t_sell.filled_at LIKE ? AND t_buy.side='BUY' AND t_buy.status='FILLED'",
            (f"{today}%",),
        ).fetchall()
    return sum((r["sell_price"] - r["buy_price"]) * r["qty"] for r in rows)
