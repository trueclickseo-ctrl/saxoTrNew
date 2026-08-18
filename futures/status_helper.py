"""
futures/status_helper.py
------------------------
Called by dashboard_futures.ps1. Outputs a single JSON object with all
data needed to render the dashboard: positions, recent trades, log health,
account equity.

Usage:
    python -X utf8 futures/status_helper.py
"""
import json, os, sys, glob
from datetime import date, datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

import saxo_auth
import requests

BASE_URL = "https://gateway.saxobank.com/sim/openapi"
DATA_DIR = os.path.join(_ROOT, "data")
LOG_DIR  = os.path.join(_ROOT, "logs")


def _get(path, params=None):
    token = saxo_auth.get_valid_access_token()
    resp  = requests.get(BASE_URL + path, headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _account():
    try:
        bal = _get("/port/v1/balances/me")
        return round(float(bal.get("TotalValue", 0)), 2)
    except Exception:
        return None


def _live_price(uic, asset_type):
    try:
        info = _get(f"/trade/v1/infoprices/", {"Uic": uic, "AssetType": asset_type,
                                                 "FieldGroups": "Quote"})
        q = info.get("Quote", {})
        mid = (q.get("Ask", 0) + q.get("Bid", 0)) / 2
        return round(mid, 6) if mid else None
    except Exception:
        return None


def _positions():
    path = os.path.join(DATA_DIR, "futures_state.json")
    if not os.path.exists(path):
        return []
    state = json.load(open(path, encoding="utf-8"))
    today = date.today()
    rows  = []
    for key, pos in state.get("positions", {}).items():
        strat, sym = key.split(":", 1) if ":" in key else ("?", key)
        entry_date = pos.get("entry_date", "?")
        try:
            days = (today - date.fromisoformat(entry_date)).days
        except Exception:
            days = 0
        live_px = _live_price(pos.get("uic"), pos.get("asset_type"))
        ep = pos.get("entry_price", 0)
        if live_px and ep:
            pnl_pct = (live_px - ep) / ep * 100 if pos.get("direction") == "Buy" \
                      else (ep - live_px) / ep * 100
        else:
            pnl_pct = None
        rows.append({
            "key":         key,
            "strategy":    strat,
            "symbol":      sym,
            "direction":   pos.get("direction", "?"),
            "quantity":    pos.get("quantity", 0),
            "entry_price": ep,
            "live_price":  live_px,
            "stop_price":  pos.get("stop_price"),
            "entry_date":  entry_date,
            "days_open":   days,
            "pnl_pct":     round(pnl_pct, 2) if pnl_pct is not None else None,
        })
    return rows


def _recent_trades(n=15):
    path = os.path.join(DATA_DIR, "trades_futures.csv")
    if not os.path.exists(path):
        return []
    rows = []
    try:
        import csv
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            all_rows = list(reader)
        for r in reversed(all_rows[-n:]):
            rows.append({
                "timestamp": r.get("timestamp", ""),
                "strategy":  r.get("strategy", ""),
                "symbol":    r.get("symbol", ""),
                "side":      r.get("side", ""),
                "quantity":  r.get("quantity", ""),
                "price":     r.get("price", ""),
                "mode":      r.get("mode", ""),
                "notes":     r.get("notes", ""),
            })
    except Exception:
        pass
    return rows


def _log_health():
    today_log = os.path.join(LOG_DIR, f"futures_{date.today():%Y-%m-%d}.log")
    pattern   = os.path.join(LOG_DIR, "futures_*.log")
    all_logs  = sorted(glob.glob(pattern))
    last_log  = all_logs[-1] if all_logs else None

    if last_log and os.path.exists(last_log):
        mtime = os.path.getmtime(last_log)
        age_min = round((datetime.now().timestamp() - mtime) / 60, 1)
        ran_today = os.path.basename(last_log) == f"futures_{date.today():%Y-%m-%d}.log"
        return {
            "log_file":       os.path.basename(last_log),
            "log_age_minutes": age_min,
            "ran_today":      ran_today,
        }
    return {"log_file": None, "log_age_minutes": None, "ran_today": False}


def main():
    equity    = _account()
    positions = _positions()
    trades    = _recent_trades()
    health    = _log_health()
    total_slots = 30
    open_slots  = total_slots - len(positions)

    out = {
        "equity":      equity,
        "total_slots": total_slots,
        "open_slots":  open_slots,
        "positions":   positions,
        "trades":      trades,
        "health":      health,
        "as_of":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
