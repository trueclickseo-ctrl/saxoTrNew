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

    print(json.dumps(result, default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
