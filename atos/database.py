"""
atos/database.py
-----------------
SQLite database -- the permanent memory of ATOS.
Auto-creates all tables on first run. No setup required.

Tables:
  trades            Every trade ever placed (or blocked)
  signals           Every signal generated (even if not traded)
  detector_weights  Algorithm weight evolution over time
  equity_curve      Daily equity snapshot per market group
  market_allocation Daily capital allocation per market
"""

import sqlite3
import os
from datetime import datetime, date

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "atos_live.db")



def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # safe concurrent reads
    return conn


def init_db():
    """Create all tables if they don't exist. Safe to call on every run."""
    with _conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy         TEXT    NOT NULL DEFAULT 'ATOS_v1',
            market_group     TEXT    NOT NULL,
            ticker           TEXT    NOT NULL,
            direction        TEXT    NOT NULL DEFAULT 'BUY',
            entry_date       TEXT    NOT NULL,
            exit_date        TEXT,
            entry_price      REAL    NOT NULL,
            exit_price       REAL,
            shares           REAL    NOT NULL,
            pnl_sek          REAL,
            commission_sek   REAL    DEFAULT 0,
            entry_score      REAL,
            d1_trend         REAL,
            d2_momentum      REAL,
            d3_breakout      REAL,
            d4_mean_revert   REAL,
            d5_volume        REAL,
            exit_reason      TEXT,
            was_profitable   INTEGER,
            stop_price       REAL,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS signals (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date      TEXT    NOT NULL,
            market_group     TEXT    NOT NULL,
            ticker           TEXT    NOT NULL,
            final_score      REAL    NOT NULL,
            d1_trend         REAL,
            d2_momentum      REAL,
            d3_breakout      REAL,
            d4_mean_revert   REAL,
            d5_volume        REAL,
            action           TEXT    NOT NULL,
            executed         INTEGER NOT NULL DEFAULT 0,
            block_reason     TEXT,
            created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS detector_weights (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at       TEXT    NOT NULL,
            num_trades_used  INTEGER NOT NULL DEFAULT 0,
            w_trend          REAL    NOT NULL DEFAULT 1.0,
            w_momentum       REAL    NOT NULL DEFAULT 1.0,
            w_breakout       REAL    NOT NULL DEFAULT 1.0,
            w_mean_revert    REAL    NOT NULL DEFAULT 1.0,
            w_volume         REAL    NOT NULL DEFAULT 1.0,
            note             TEXT
        );

        CREATE TABLE IF NOT EXISTS equity_curve (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            snap_date        TEXT    NOT NULL UNIQUE,
            total_equity_sek REAL    NOT NULL,
            us_equity_sek    REAL    DEFAULT 0,
            omx30_equity_sek REAL    DEFAULT 0,
            dax_equity_sek   REAL    DEFAULT 0,
            commodities_sek  REAL    DEFAULT 0,
            forex_sek        REAL    DEFAULT 0,
            open_positions   INTEGER DEFAULT 0,
            trades_today     INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS market_allocation (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            alloc_date       TEXT    NOT NULL,
            market_group     TEXT    NOT NULL,
            allocated_pct    REAL    NOT NULL,
            capital_sek      REAL    NOT NULL,
            win_rate         REAL,
            profit_factor    REAL,
            note             TEXT,
            UNIQUE(alloc_date, market_group)
        );
        """)
    migrate_schema()

def migrate_schema():
    """Add columns for ATOS v2 detectors. Safe to call multiple times."""
    migrations = [
        "ALTER TABLE trades ADD COLUMN d6_smart_money REAL",
        "ALTER TABLE trades ADD COLUMN d7_mom_quality REAL",
        "ALTER TABLE trades ADD COLUMN d8_regime REAL",
        "ALTER TABLE trades ADD COLUMN trailing_stop_high REAL",
        "ALTER TABLE trades ADD COLUMN regime_at_entry TEXT",
        "ALTER TABLE signals ADD COLUMN d6_smart_money REAL",
        "ALTER TABLE signals ADD COLUMN d7_mom_quality REAL",
        "ALTER TABLE signals ADD COLUMN d8_regime REAL",
        "ALTER TABLE signals ADD COLUMN regime TEXT",
        "ALTER TABLE detector_weights ADD COLUMN w_smart_money REAL DEFAULT 1.0",
        "ALTER TABLE detector_weights ADD COLUMN w_mom_quality REAL DEFAULT 1.0",
        "ALTER TABLE detector_weights ADD COLUMN w_regime REAL DEFAULT 1.0",
        "ALTER TABLE equity_curve ADD COLUMN cph25_equity_sek REAL DEFAULT 0",
        # Logging-parity additions: per-signal strategy tag + reversion indicators
        "ALTER TABLE signals ADD COLUMN strategy TEXT DEFAULT 'ATOS_v1'",
        "ALTER TABLE signals ADD COLUMN scan_ts TEXT",
        "ALTER TABLE signals ADD COLUMN rsi REAL",
        "ALTER TABLE signals ADD COLUMN dip_pct REAL",
        "ALTER TABLE signals ADD COLUMN vol_ratio REAL",
        # 2026-09-01: SIM paper-fill fallback. paper=1 means Saxo SIM's order
        # engine rejected the real order (CouldNotCompleteRequest 90) and the
        # trade was booked LOCALLY, managed by ATOS's own should_exit() logic.
        # It has no Saxo counterpart -- housekeeping's StocksAdapter skips it.
        "ALTER TABLE trades ADD COLUMN paper INTEGER DEFAULT 0",
        # 2026-09-01: the broker-side protective stop's order id, or NULL if
        # it was never placed / rejected (the "NotOwned" settlement race).
        # NULL on an open non-paper row -> atos_runner._heal_missing_stock_stops
        # re-attempts it each cycle.
        "ALTER TABLE trades ADD COLUMN stop_order_id TEXT",
    ]
    with _conn() as conn:
        for sql in migrations:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # column already exists

# -- Trades ---------------------------------------------------------

def insert_trade(data: dict) -> int:
    """Insert a new open trade. Returns the trade id.
    `paper` (default 0): 1 == a locally-simulated fill because Saxo SIM
    rejected the real order (see migrate_schema).
    `stop_order_id` (default None): the broker stop's order id, or None if
    it wasn't placed -- _heal_missing_stock_stops re-attempts a None one."""
    data = {"strategy": "ATOS_v1", "paper": 0, "stop_order_id": None, **data}
    with _conn() as conn:
        cur = conn.execute("""
            INSERT INTO trades
              (strategy, market_group, ticker, direction, entry_date, entry_price, shares,
               commission_sek, entry_score, d1_trend, d2_momentum, d3_breakout,
               d4_mean_revert, d5_volume, d6_smart_money, d7_mom_quality, d8_regime,
               trailing_stop_high, regime_at_entry, stop_price, paper, stop_order_id)
            VALUES
              (:strategy, :market_group, :ticker, :direction, :entry_date, :entry_price, :shares,
               :commission_sek, :entry_score, :d1_trend, :d2_momentum, :d3_breakout,
               :d4_mean_revert, :d5_volume, :d6_smart_money, :d7_mom_quality, :d8_regime,
               :trailing_stop_high, :regime_at_entry, :stop_price, :paper, :stop_order_id)
        """, data)
        return cur.lastrowid


def set_stop_order_id(trade_id: int, stop_order_id: str) -> None:
    """Record that a broker-side protective stop now covers this trade."""
    with _conn() as conn:
        conn.execute("UPDATE trades SET stop_order_id = ? WHERE id = ?",
                     (stop_order_id, trade_id))


def close_trade(trade_id: int, exit_price: float, exit_reason: str, pnl_sek: float,
                commission_sek: float):
    """Mark an open trade as closed."""
    profitable = 1 if pnl_sek > 0 else 0
    with _conn() as conn:
        conn.execute("""
            UPDATE trades
               SET exit_date = ?, exit_price = ?, exit_reason = ?,
                   pnl_sek = ?, commission_sek = commission_sek + ?,
                   was_profitable = ?
             WHERE id = ?
        """, (date.today().isoformat(), exit_price, exit_reason,
              pnl_sek, commission_sek, profitable, trade_id))


def get_open_trades() -> list[dict]:
    """Return all trades without an exit date."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE exit_date IS NULL ORDER BY entry_date"
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_closed_trades(n: int = 100) -> list[dict]:
    """Return the N most recent closed trades for weight learning."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM trades
             WHERE exit_date IS NOT NULL AND was_profitable IS NOT NULL
             ORDER BY exit_date DESC LIMIT ?
        """, (n,)).fetchall()
        return [dict(r) for r in rows]


def get_all_closed_trades() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE exit_date IS NOT NULL ORDER BY exit_date"
        ).fetchall()
        return [dict(r) for r in rows]


# -- Signals --------------------------------------------------------

def insert_signal(data: dict):
    data = {
        "strategy":     "ATOS_v1",
        "scan_ts":      None,
        "rsi":          None,
        "dip_pct":      None,
        "vol_ratio":    None,
        **data,
    }
    with _conn() as conn:
        conn.execute("""
            INSERT INTO signals
              (signal_date, scan_ts, strategy, market_group, ticker, final_score,
               d1_trend, d2_momentum, d3_breakout, d4_mean_revert, d5_volume,
               d6_smart_money, d7_mom_quality, d8_regime, regime,
               action, executed, block_reason,
               rsi, dip_pct, vol_ratio)
            VALUES
              (:signal_date, :scan_ts, :strategy, :market_group, :ticker, :final_score,
               :d1_trend, :d2_momentum, :d3_breakout, :d4_mean_revert, :d5_volume,
               :d6_smart_money, :d7_mom_quality, :d8_regime, :regime,
               :action, :executed, :block_reason,
               :rsi, :dip_pct, :vol_ratio)
        """, data)


# -- Detector weights -----------------------------------------------

def get_current_weights() -> dict:
    """Return the most recent weight row, or equal defaults if none exists."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM detector_weights ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if row:
        row_dict = dict(row)
        return {
            "w_trend":       row_dict.get("w_trend", 1.0),
            "w_momentum":    row_dict.get("w_momentum", 1.0),
            "w_breakout":    row_dict.get("w_breakout", 1.0),
            "w_mean_revert": row_dict.get("w_mean_revert", 1.0),
            "w_volume":      row_dict.get("w_volume", 1.0),
            "w_smart_money": row_dict.get("w_smart_money", 1.0),
            "w_mom_quality": row_dict.get("w_mom_quality", 1.0),
            "w_regime":      row_dict.get("w_regime", 1.0),
            "num_trades":    row_dict.get("num_trades_used", 0),
        }
    # First run -- return equal weights
    return {"w_trend": 1.0, "w_momentum": 1.0, "w_breakout": 1.0,
            "w_mean_revert": 1.0, "w_volume": 1.0, "w_smart_money": 1.0,
            "w_mom_quality": 1.0, "w_regime": 1.0, "num_trades": 0}


def save_weights(weights: dict, num_trades: int, note: str = ""):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO detector_weights
              (updated_at, num_trades_used, w_trend, w_momentum,
               w_breakout, w_mean_revert, w_volume, w_smart_money, w_mom_quality, w_regime, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datetime.now().isoformat(timespec="seconds"), num_trades,
              weights["w_trend"], weights["w_momentum"], weights["w_breakout"],
              weights["w_mean_revert"], weights["w_volume"], weights.get("w_smart_money", 1.0),
              weights.get("w_mom_quality", 1.0), weights.get("w_regime", 1.0), note))


# -- Equity curve ---------------------------------------------------

def upsert_equity(data: dict):
    with _conn() as conn:
        conn.execute("""
            INSERT INTO equity_curve
              (snap_date, total_equity_sek, us_equity_sek, omx30_equity_sek,
               cph25_equity_sek, dax_equity_sek, commodities_sek, forex_sek,
               open_positions, trades_today)
            VALUES
              (:snap_date, :total_equity_sek, :us_equity_sek, :omx30_equity_sek,
               :cph25_equity_sek, :dax_equity_sek, :commodities_sek, :forex_sek,
               :open_positions, :trades_today)
            ON CONFLICT(snap_date) DO UPDATE SET
              total_equity_sek = excluded.total_equity_sek,
              us_equity_sek    = excluded.us_equity_sek,
              omx30_equity_sek = excluded.omx30_equity_sek,
              cph25_equity_sek = excluded.cph25_equity_sek,
              dax_equity_sek   = excluded.dax_equity_sek,
              commodities_sek  = excluded.commodities_sek,
              forex_sek        = excluded.forex_sek,
              open_positions   = excluded.open_positions,
              trades_today     = excluded.trades_today
        """, data)


def get_equity_curve(days: int = 90) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM equity_curve
             ORDER BY snap_date DESC LIMIT ?
        """, (days,)).fetchall()
        return [dict(r) for r in reversed(rows)]


def get_weight_history() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM detector_weights ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
