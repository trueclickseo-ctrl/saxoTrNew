"""
avanza_dashboard_helper.py
--------------------------
Prints a JSON blob to stdout for dashboard_avanza.ps1 to consume.
Never places orders. Safe to run at any time.

Usage:
    python avanza_module/avanza_dashboard_helper.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _load_env() -> None:
    env_file = os.path.join(_ROOT, ".env.avanza")
    if not os.path.exists(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> None:
    _load_env()

    from avanza_module import avanza_client as ac
    from avanza_module import avanza_state as st

    result: dict = {}

    # ── Status file (from last run_avanza.py run) ─────────────────────────────
    status = st.read_status()
    result["last_run"] = status.get("timestamp", "")
    result["signal_tickers"] = status.get("signal_tickers", [])
    result["signal_source"]  = status.get("signal_source", "")
    result["budget_sek"]     = status.get("budget_sek", 0)

    # ── Minutes since last run ────────────────────────────────────────────────
    if result["last_run"]:
        try:
            last_dt = datetime.fromisoformat(result["last_run"])
            diff_min = (datetime.now() - last_dt).total_seconds() / 60
            result["last_run_mins_ago"] = round(diff_min, 1)
        except Exception:
            result["last_run_mins_ago"] = None
    else:
        result["last_run_mins_ago"] = None

    # ── Recent trades from DB ─────────────────────────────────────────────────
    try:
        recent = st.get_recent_trades(10)
        result["recent_trades"] = recent
        result["today_pnl_sek"] = round(st.get_today_pnl_sek(), 2)
    except Exception:
        result["recent_trades"] = []
        result["today_pnl_sek"] = 0.0

    # ── Live Avanza account data (requires credentials) ───────────────────────
    try:
        client     = ac.get_client()
        account_id = os.environ.get("AVANZA_ACCOUNT_ID") or ac.get_isk_account_id(client)
        summary    = ac.get_account_summary(client, account_id)
        positions  = ac.get_positions(client, account_id)
        orders     = ac.get_open_orders(client)

        result["account_id"]        = account_id
        result["account_type"]      = summary.get("account_type", "")
        result["value_sek"]         = summary.get("value_sek", 0)
        result["buying_power_sek"]  = summary.get("buying_power_sek", 0)
        result["total_profit_pct"]  = summary.get("total_profit_pct", 0)
        result["positions"]         = positions
        result["open_orders"]       = orders
        result["live_ok"]           = True

    except Exception as exc:
        result["live_ok"]  = False
        result["live_err"] = str(exc)
        result["positions"] = []
        result["open_orders"] = []
        result["value_sek"] = 0
        result["buying_power_sek"] = 0

    # ── Ledger positions with days held ──────────────────────────────────────
    try:
        import sqlite3 as _sqlite3
        from datetime import date as _date
        _db = os.path.join(_ROOT, "data", "avanza_trades.db")
        _con = _sqlite3.connect(_db)
        _con.row_factory = _sqlite3.Row
        _rows = _con.execute(
            "SELECT ticker, qty, entry_price, entry_date, stop_price, "
            "trailing_stop_high, stop_order_id FROM trades "
            "WHERE status='FILLED' AND side='BUY' ORDER BY entry_date"
        ).fetchall()
        _con.close()

        today = _date.today()
        ledger = []
        for _r in _rows:
            d = dict(_r)
            try:
                buy_date = _date.fromisoformat(str(d["entry_date"])[:10])
                days_held = (today - buy_date).days
            except Exception:
                buy_date = None
                days_held = None
            stop = d.get("stop_price") or 0.0
            entry = d.get("entry_price") or 0.0
            stop_pct = round((entry - stop) / entry * 100, 1) if entry else None
            ledger.append({
                "ticker":       d["ticker"],
                "qty":          d["qty"],
                "entry_price":  entry,
                "entry_date":   str(d["entry_date"])[:10] if d["entry_date"] else None,
                "days_held":    days_held,
                "stop_price":   stop,
                "stop_pct":     stop_pct,
                "trail_high":   d.get("trailing_stop_high") or entry,
            })

        # Next rebalance check = 14 days from oldest open position's buy date
        from datetime import timedelta as _td
        buy_dates = [
            _date.fromisoformat(h["entry_date"])
            for h in ledger if h.get("entry_date")
        ]
        if buy_dates:
            oldest_buy = min(buy_dates)
            next_rebalance_dt = oldest_buy + _td(days=14)
            days_to_rebalance = (next_rebalance_dt - today).days
            next_rebalance = next_rebalance_dt.isoformat()
        else:
            next_rebalance = None
            days_to_rebalance = None

        result["ledger_positions"]  = ledger
        result["next_rebalance"]    = next_rebalance
        result["days_to_rebalance"] = days_to_rebalance
    except Exception as exc:
        result["ledger_positions"]  = []
        result["next_rebalance"]    = None
        result["days_to_rebalance"] = None
        result["ledger_err"]        = str(exc)

    print(json.dumps(result, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
