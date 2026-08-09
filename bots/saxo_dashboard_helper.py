"""
bots/saxo_dashboard_helper.py
------------------------------
Data gatherer for dashboard_saxo.ps1.

Reads local SQLite database, log files, and optionally the Saxo API
for live balance / positions (falls back gracefully if the token has expired).

Never places orders or writes any files except printing JSON to stdout.

Usage (called by dashboard_saxo.ps1 -- don't run manually unless debugging):
    .venv/Scripts/python bots/saxo_dashboard_helper.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, date, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR   = ROOT / "data"
DB_PATH    = DATA_DIR / "atos_live.db"
SCAN_STATE = DATA_DIR / "atos_scan_state.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _us_market_open(now_utc: datetime | None = None) -> tuple[bool, str]:
    """Return (is_open, status_str) for NYSE/NASDAQ.
    Uses a simplified EDT/EST approximation (no holiday calendar).
    """
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    wd = now_utc.weekday()   # 0=Mon, 6=Sun
    if wd >= 5:
        return False, "weekend"

    m, d = now_utc.month, now_utc.day
    # Rough DST boundary: EDT from ~Mar 8 to ~Nov 7
    is_edt = (4 <= m <= 10) or (m == 3 and d >= 8) or (m == 11 and d < 7)
    open_utc  = timedelta(hours=13 if is_edt else 14, minutes=30)
    close_utc = timedelta(hours=20 if is_edt else 21)

    tod = timedelta(hours=now_utc.hour, minutes=now_utc.minute)
    if open_utc <= tod < close_utc:
        oh = int(open_utc.total_seconds() // 3600)
        om = int((open_utc.total_seconds() % 3600) // 60)
        ch = int(close_utc.total_seconds() // 3600)
        cm = int((close_utc.total_seconds() % 3600) // 60)
        return True, f"OPEN ({oh:02d}:{om:02d}-{ch:02d}:{cm:02d} UTC)"
    elif tod < open_utc:
        oh = int(open_utc.total_seconds() // 3600)
        om = int((open_utc.total_seconds() % 3600) // 60)
        return False, f"pre-market (opens {oh:02d}:{om:02d} UTC)"
    else:
        return False, "after-hours"


def _read_db(query: str, params: tuple = ()) -> list[dict]:
    """Run a read-only SQLite query. Returns [] on any failure."""
    import sqlite3
    if not DB_PATH.exists():
        return []
    try:
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _bot_health() -> dict:
    """Determine if atos_runner.py ran today / recently by checking engine logs."""
    today_str     = date.today().isoformat()
    yesterday_str = (date.today() - timedelta(days=1)).isoformat()

    log_age_minutes = None
    log_file        = None
    last_scan_date  = None

    for date_str in [today_str, yesterday_str]:
        candidate = DATA_DIR / f"engine_{date_str}.log"
        if candidate.exists():
            age_sec         = time.time() - candidate.stat().st_mtime
            log_age_minutes = round(age_sec / 60, 1)
            log_file        = str(candidate)
            last_scan_date  = date_str
            break

    ran_today = (DATA_DIR / f"engine_{today_str}.log").exists()

    # Determine status
    if ran_today:
        status = "RAN_TODAY"
    elif last_scan_date == yesterday_str:
        status = "RAN_YESTERDAY"
    else:
        status = "UNKNOWN"

    return {
        "status":           status,
        "ran_today":        ran_today,
        "log_file":         log_file,
        "log_age_minutes":  log_age_minutes,
        "last_scan_date":   last_scan_date,
    }


def _last_scan() -> dict:
    """Read scan state from atos_scan_state.json (written by atos_runner.py)."""
    if not SCAN_STATE.exists():
        return {"available": False}
    try:
        with open(SCAN_STATE, encoding="utf-8") as f:
            state = json.load(f)
        state["available"] = True
        ts = state.get("scan_ts")
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                state["minutes_ago"] = round(
                    (datetime.now(timezone.utc) - dt).total_seconds() / 60, 1
                )
            except Exception:
                state["minutes_ago"] = None
        return state
    except Exception:
        return {"available": False}


def _saxo_live_balance() -> dict | None:
    """Try Saxo API for live balance. Returns None if token expired."""
    try:
        import saxo_client
        bal = saxo_client.get_balances()
        return {
            "total_sek":  round(float(bal.get("TotalValue", 0)), 2),
            "cash_sek":   round(float(bal.get("CashAvailableForTrading", 0)), 2),
            "margin_sek": round(float(bal.get("MarginAvailableForTrading", 0)), 2),
            "source":     "live",
        }
    except Exception:
        return None


def _saxo_live_positions() -> list[dict] | None:
    """Try Saxo API for live open positions. Returns None if token expired."""
    try:
        import saxo_client
        resp = saxo_client.get_positions()
        rows = resp.get("Data", []) if isinstance(resp, dict) else []
        positions = []
        for pos in rows:
            net = pos.get("NetPositionBase", {})
            view = pos.get("NetPositionView", {})
            positions.append({
                "symbol":            pos.get("DisplayAndFormat", {}).get("Symbol", "?"),
                "description":       pos.get("DisplayAndFormat", {}).get("Description", ""),
                "net_qty":           net.get("Amount", 0),
                "entry_price":       net.get("OpenPrice", None),
                "current_price":     view.get("LastTraded", None) or view.get("Ask", None),
                "unrealized_pnl":    view.get("ProfitLossOnTrade", None),
                "unrealized_pnl_pct": view.get("ProfitLossOnTradeInBaseCurrency", None),
                "currency":          pos.get("DisplayAndFormat", {}).get("Currency", "USD"),
                "source":            "live",
            })
        return positions
    except Exception:
        return None


def _open_positions_from_db() -> list[dict]:
    """Open trades from atos_live.db as fallback when Saxo API is unavailable."""
    rows = _read_db(
        "SELECT * FROM trades WHERE exit_date IS NULL ORDER BY entry_date DESC"
    )
    result = []
    for r in rows:
        entry_d = r.get("entry_date")
        result.append({
            "ticker":       r.get("ticker"),
            "strategy":     r.get("strategy"),
            "market_group": r.get("market_group"),
            "shares":       r.get("shares"),
            "entry_price":  r.get("entry_price"),
            "entry_date":   entry_d,
            "stop_price":   r.get("stop_price"),
            "days_open":    (date.today() - date.fromisoformat(entry_d)).days
                            if entry_d else None,
            "source":       "db",
        })
    return result


def _recent_trades(limit: int = 20) -> list[dict]:
    """Most recent closed trades from DB."""
    return _read_db("""
        SELECT ticker, strategy, direction, entry_date, exit_date,
               entry_price, exit_price, shares, pnl_sek, exit_reason,
               commission_sek
          FROM trades
         WHERE exit_date IS NOT NULL
         ORDER BY exit_date DESC, id DESC
         LIMIT ?
    """, (limit,))


def _today_signals() -> list[dict]:
    """All signals from the signals table for today's scan date."""
    today = date.today().isoformat()
    rows = _read_db("""
        SELECT ticker, strategy, action, executed, block_reason,
               rsi, dip_pct, vol_ratio, final_score, scan_ts, market_group
          FROM signals
         WHERE signal_date = ?
         ORDER BY strategy, action, final_score DESC
    """, (today,))
    return rows


def gather() -> dict:
    now_utc = datetime.now(timezone.utc)
    market_open, market_status = _us_market_open(now_utc)

    # ── Balance: try live Saxo API, fall back to DB equity curve ─────────────
    balance = _saxo_live_balance()
    if balance is None:
        eq_rows = _read_db(
            "SELECT total_equity_sek FROM equity_curve ORDER BY id DESC LIMIT 1"
        )
        if eq_rows:
            balance = {
                "total_sek":  round(eq_rows[0]["total_equity_sek"], 2),
                "cash_sek":   None,
                "margin_sek": None,
                "source":     "db_last_snapshot",
            }

    # ── Positions: try live, fall back to DB ─────────────────────────────────
    live_positions = _saxo_live_positions()
    if live_positions is not None:
        positions = live_positions
    else:
        positions = _open_positions_from_db()

    # Slot summary from DB (strategy-aware)
    open_db = _open_positions_from_db()
    blend_open  = [p for p in open_db if (p.get("strategy") or "") == "US Blend"]
    rev_open    = [p for p in open_db if (p.get("strategy") or "") == "US Reversion"]

    signals_today = _today_signals()
    rev_signals   = [s for s in signals_today if s.get("strategy") == "US Reversion"]
    blend_signals = [s for s in signals_today if s.get("strategy") == "US Blend"]

    return {
        "ts":     _now_iso(),
        "market": {"open": market_open, "status": market_status},
        "balance": balance,
        "positions": positions,
        "slots": {
            "blend": {"open": len(blend_open), "tickers": [p["ticker"] for p in blend_open]},
            "reversion": {"open": len(rev_open), "tickers": [p["ticker"] for p in rev_open]},
        },
        "recent_trades":   _recent_trades(20),
        "signals_today":   signals_today,
        "signals_summary": {
            "reversion_scanned": len(rev_signals),
            "reversion_buy":     sum(1 for s in rev_signals if s["action"] == "BUY"),
            "reversion_executed": sum(1 for s in rev_signals if s["executed"]),
            "reversion_skip":    sum(1 for s in rev_signals if s["action"] == "SKIP"),
            "blend_buy":         sum(1 for s in blend_signals if s["action"] == "BUY"),
        },
        "last_scan": _last_scan(),
        "health":    _bot_health(),
        "error":     None,
    }


if __name__ == "__main__":
    try:
        data = gather()
    except Exception as exc:
        data = {"ts": _now_iso(), "error": str(exc), "positions": [], "balance": None}
    print(json.dumps(data, indent=None, default=str))
