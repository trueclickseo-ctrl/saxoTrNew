"""
cancel_orphan_stops.py
----------------------
Cancel the 17 GTC stop-loss orders left at IBKR after the blend/scorer
collision incident (2026-09-04).

These stops were placed when the scorer bought the stocks. The blend
rebalancer then sold those positions via market orders, but the GTC stops
remain open at IBKR orphaned -- there are no matching positions or local
DB records behind them.

Symbols:
    COIN CRM DASH HOOD MPC MRK NEM NOW NVDA PLTR SMCI SNOW TEAM TSLA VEEV VLO WDAY

Usage (dry-run first):
    python scripts/cancel_orphan_stops.py

Actually cancel:
    python scripts/cancel_orphan_stops.py --execute
"""

import argparse
import json
import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ibkr_module import ibkr_client as ic

ORPHAN_SYMBOLS = {
    "COIN", "CRM", "DASH", "HOOD", "MPC", "MRK", "NEM", "NOW",
    "NVDA", "PLTR", "SMCI", "SNOW", "TEAM", "TSLA", "VEEV", "VLO", "WDAY",
}

_CFG_PATH = os.path.join(ROOT, "ibkr_module", "config", "ibkr_config.json")


def _load_config() -> dict:
    with open(_CFG_PATH) as f:
        return json.load(f)


def main(execute: bool) -> None:
    cfg = _load_config()
    port = cfg["port_paper"]
    host = cfg["host"]
    client_id = 19  # unused slot — avoids colliding with any running module

    print(f"Connecting to IB Gateway {host}:{port} (clientId={client_id}) ...")
    ib = ic.connect(host, port, client_id=client_id)
    print("Connected.\n")

    # Pull all open orders
    ib.reqOpenOrders()
    ib.sleep(1.5)
    open_trades = list(ib.openTrades())

    if not open_trades:
        print("No open orders found at IBKR broker.")
        ic.disconnect(ib)
        return

    print(f"Found {len(open_trades)} open order(s) total at IBKR.\n")

    # Filter: stop orders on the orphan symbols
    targets = []
    for trade in open_trades:
        sym = trade.contract.symbol
        otype = trade.order.orderType.upper()  # "STP" or "STOP"
        action = trade.order.action.upper()
        status = trade.orderStatus.status

        if sym in ORPHAN_SYMBOLS and "STP" in otype and action == "SELL":
            targets.append(trade)
            print(
                f"  [{sym}]  orderType={otype}  action={action}  "
                f"qty={trade.order.totalQuantity}  "
                f"stopPrice={trade.order.auxPrice}  "
                f"status={status}  orderId={trade.order.orderId}"
            )

    if not targets:
        print("No orphan stop orders matched. Nothing to cancel.")
        print("\nAll open orders (for reference):")
        for t in open_trades:
            print(
                f"  [{t.contract.symbol}] {t.order.orderType} {t.order.action} "
                f"qty={t.order.totalQuantity}  status={t.orderStatus.status}  "
                f"orderId={t.order.orderId}"
            )
        ic.disconnect(ib)
        return

    print(f"\n{'DRY RUN' if not execute else 'EXECUTING'}: {len(targets)} stop order(s) to cancel.\n")

    if not execute:
        print("Add --execute to actually cancel.")
        ic.disconnect(ib)
        return

    cancelled = 0
    for trade in targets:
        sym = trade.contract.symbol
        try:
            ib.cancelOrder(trade.order)
            ib.sleep(0.5)
            print(f"  Cancelled [{sym}]  orderId={trade.order.orderId}")
            cancelled += 1
        except Exception as e:
            print(f"  ERROR cancelling [{sym}] orderId={trade.order.orderId}: {e}")

    ib.sleep(1.0)
    ic.disconnect(ib)
    print(f"\nDone. {cancelled}/{len(targets)} orders cancelled.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cancel orphan scorer stop orders at IBKR.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually cancel orders (default: dry-run)")
    args = parser.parse_args()
    main(execute=args.execute)
