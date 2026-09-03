"""
close_atos_sim_orphans.py
--------------------------
Find Saxo SIM stock positions that have no record in data/atos_live.db
(orphans — opened outside ATOS or from a crashed run that lost state)
and close them with market sell orders.

Dry-run by default (prints what it would do). Pass --execute to place real
SIM sell orders.

Usage:
    python close_atos_sim_orphans.py             # dry-run, shows orphans
    python close_atos_sim_orphans.py --execute   # closes them on Saxo SIM
"""

import os
import sys
import sqlite3

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

DB_PATH = os.path.join(BASE_DIR, "data", "atos_live.db")

_STOCK_ASSET_TYPES = {"Stock", "StockIndex"}

try:
    from atos.universe import ATOS_UNIVERSE as _ATOS_UNIVERSE
except Exception:
    _ATOS_UNIVERSE = None


def _db_tickers() -> set:
    if not os.path.exists(DB_PATH):
        return set()
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT ticker FROM trades WHERE exit_date IS NULL").fetchall()
        return {r[0].upper() for r in rows}
    except Exception:
        return set()
    finally:
        conn.close()


def _saxo_orphans(token: str) -> list:
    """Return [{ticker, uic, shares, entry, pnl}] for Saxo SIM stocks not in DB."""
    if not token:
        print("No SIM token — cannot fetch positions.")
        return []
    import requests
    r = requests.get(
        "https://gateway.saxobank.com/sim/openapi/port/v1/positions/me",
        headers={"Authorization": f"Bearer {token}"},
        params={"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"},
        timeout=10,
    )
    if r.status_code != 200:
        print(f"Saxo positions returned {r.status_code}: {r.text[:200]}")
        return []

    db_tickers = _db_tickers()
    out = []
    for p in r.json().get("Data", []):
        pb   = p.get("PositionBase", {})
        pv   = p.get("PositionView", {})
        disp = p.get("DisplayAndFormat", {})
        if pb.get("AssetType") not in _STOCK_ASSET_TYPES:
            continue
        sym  = (disp.get("Symbol") or pb.get("Symbol") or "")
        base = sym.split(":")[0].upper()
        if _ATOS_UNIVERSE is not None and base not in _ATOS_UNIVERSE:
            continue
        if base in db_tickers:
            continue
        out.append({
            "ticker":    base,
            "uic":       pb.get("Uic"),
            "shares":    int(pb.get("Amount") or 0),
            "entry":     float(pb.get("OpenPrice") or 0),
            "pnl":       float(pv.get("ProfitLossOnTrade") or 0),
            "position_id": pb.get("PositionId"),
        })
    return out


def main():
    execute = "--execute" in sys.argv

    import price_service
    token = price_service.load_token()

    orphans = _saxo_orphans(token)
    if not orphans:
        print("No orphan SIM positions found.")
        return

    print(f"\nOrphan SIM stock positions (in Saxo, not in atos_live.db): {len(orphans)}")
    print(f"  {'Ticker':<8} {'Shares':>6}  {'Entry':>8}  {'P&L USD':>9}")
    print(f"  {'─'*40}")
    for o in orphans:
        print(f"  {o['ticker']:<8} {o['shares']:>6}  ${o['entry']:>7.2f}  {o['pnl']:>+9.2f}")

    if not execute:
        print(f"\nDry-run — no orders placed.")
        print(f"Run with --execute to close these {len(orphans)} position(s) on Saxo SIM.")
        return

    print(f"\nClosing {len(orphans)} orphan position(s) on Saxo SIM...")
    import saxo_client
    closed = 0
    for o in orphans:
        ticker = o["ticker"]
        uic    = o["uic"]
        shares = o["shares"]
        if not uic or not shares:
            print(f"  [SKIP] {ticker}: uic={uic} shares={shares}")
            continue
        try:
            saxo_client.place_market_order(uic, "Stock", "Sell", shares, env="sim")
            print(f"  [SOLD] {ticker}: {shares} shares @ market")
            closed += 1
        except Exception as e:
            print(f"  [FAIL] {ticker}: {e}")

    print(f"\nDone — closed {closed}/{len(orphans)} orphan position(s).")


if __name__ == "__main__":
    main()
