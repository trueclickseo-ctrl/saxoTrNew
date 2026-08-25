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
import pnl_tracker

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


def _account_key():
    try:
        info = _get("/port/v1/accounts/me")
        data = info.get("Data", info)
        acct = data[0] if isinstance(data, list) else data
        return acct.get("AccountKey", "") if isinstance(acct, dict) else ""
    except Exception:
        return ""

_AKEY = None

def _live_price(uic, asset_type):
    global _AKEY
    if _AKEY is None:
        _AKEY = _account_key()
    try:
        params = {"Uic": uic, "AssetType": asset_type, "FieldGroups": "Quote"}
        if _AKEY:
            params["AccountKey"] = _AKEY
        info = _get("/trade/v1/infoprices", params)
        q = info.get("Quote", {})
        ask = q.get("Ask") or q.get("AskSize")
        bid = q.get("Bid") or q.get("BidSize")
        mid = (float(q.get("Ask", 0)) + float(q.get("Bid", 0))) / 2
        return round(mid, 6) if mid else None
    except Exception:
        return None


def _saxo_live_pnl():
    """Fetch live PnL per UIC from Saxo positions endpoint."""
    uic_pnl = {}
    try:
        global _AKEY
        if _AKEY is None:
            _AKEY = _account_key()
        data = _get("/port/v1/positions/me").get("Data", [])
        for p in data:
            pb   = p.get("PositionBase", {})
            pv   = p.get("PositionView", {})
            uic  = pb.get("Uic")
            pnl  = pv.get("ProfitLossOnTrade")
            cpx  = pv.get("CurrentPrice") or 0
            qty  = pb.get("Amount", 0)
            side = pb.get("BuySell", "Buy")
            if uic is not None:
                uic_pnl[uic] = {"pnl": pnl, "current_price": cpx, "qty": qty, "side": side}
    except Exception:
        pass
    return uic_pnl


def _load_uic_cache():
    path = os.path.join(DATA_DIR, "futures_uic_cache.json")
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return {}

_UIC_BY_UIC = None  # {uic_int: info_dict}

def _contract_size_for(uic, asset_type):
    global _UIC_BY_UIC
    if _UIC_BY_UIC is None:
        cache = _load_uic_cache()
        _UIC_BY_UIC = {v["uic"]: v for v in cache.values()}
    info = _UIC_BY_UIC.get(uic, {})
    return info.get("contract_size", 1)


def _positions():
    path = os.path.join(DATA_DIR, "futures_state.json")
    if not os.path.exists(path):
        return []
    state    = json.load(open(path, encoding="utf-8"))
    today    = date.today()
    saxo_pnl = _saxo_live_pnl()
    rows     = []

    for key, pos in state.get("positions", {}).items():
        strat, sym = key.split(":", 1) if ":" in key else ("?", key)
        entry_date = pos.get("entry_date", "?")
        try:
            days = (today - date.fromisoformat(entry_date)).days
        except Exception:
            days = 0

        ep         = pos.get("entry_price", 0)
        uic        = pos.get("uic")
        qty        = pos.get("quantity", 1)
        direction  = pos.get("direction", "Buy")
        asset_type = pos.get("asset_type", "")

        # Try infoprices first (works for FxSpot/GC)
        live_px = _live_price(uic, asset_type)

        # Fall back: back-calculate from Saxo PnL
        if (live_px is None or live_px == 0) and uic in saxo_pnl:
            sp   = saxo_pnl[uic]
            cpx  = sp.get("current_price") or 0
            pnl  = sp.get("pnl")
            if cpx and cpx > 0:
                live_px = round(cpx, 6)
            elif pnl is not None and ep and qty:
                # ContractFutures: Saxo PnL is in account currency (USD).
                # live = entry ± pnl / (qty × contract_size)
                # CfdOnIndex / CdfOnEtf / FxSpot: PnL is in price-units × qty.
                # live = entry ± pnl / qty
                cs = _contract_size_for(uic, asset_type) if asset_type == "ContractFutures" else 1
                divisor = qty * cs
                if direction == "Buy":
                    live_px = round(ep + pnl / divisor, 6)
                else:
                    live_px = round(ep - pnl / divisor, 6)

        if live_px and ep:
            pnl_pct = (live_px - ep) / ep * 100 if direction == "Buy" \
                      else (ep - live_px) / ep * 100
        else:
            pnl_pct = None

        rows.append({
            "key":         key,
            "strategy":    strat,
            "symbol":      sym,
            "direction":   direction,
            "quantity":    qty,
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


def _strategy_stats() -> tuple[list, list]:
    """Strategy-wise P&L/WR/PF breakdown, mirroring forex_dashboard.py's
    STRATEGY BREAKDOWN. Added 2026-08-25 -- this dashboard never had it at
    all (only per-position rows + raw recent-trades CSV lines), so a real
    closed loss (e.g. a stop-loss caught by intraday_monitor.py) had no
    strategy-wise view showing it, even though pnl_tracker had the data."""
    try:
        all_time = pnl_tracker.get_strategy_summary("futures")
        today    = pnl_tracker.get_strategy_summary_since("futures", date.today().isoformat())
        return all_time, today
    except Exception:
        return [], []


def main():
    equity    = _account()
    positions = _positions()
    trades    = _recent_trades()
    health    = _log_health()
    strategy_stats, strategy_stats_today = _strategy_stats()
    total_slots = 30
    open_slots  = total_slots - len(positions)

    out = {
        "equity":      equity,
        "total_slots": total_slots,
        "open_slots":  open_slots,
        "positions":   positions,
        "trades":      trades,
        "health":      health,
        "strategy_stats":       strategy_stats,
        "strategy_stats_today": strategy_stats_today,
        "as_of":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
