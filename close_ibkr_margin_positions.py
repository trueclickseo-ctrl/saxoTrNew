"""
close_ibkr_margin_positions.py
-------------------------------
One-shot script: close 4 unprotected losing positions to free margin for
penny/bagger strategies.

Targets (all have stop_price=0, stop_order_id=None, and are at a loss):
  ZS  x9  (US Ensemble)       @ fill $196.50  -> ~$-16
  ZS  x9  (US Momentum)       @ fill $196.50  -> ~$-16
  ZS  x9  (scorer_portfolio)  @ fill $196.50  -> ~$-16
  ILMN x7 (US Momentum)      @ fill $274.23  -> ~$-11

Expected margin freed: ~$7,200 USD (~SEK 74,000)
New buying power (est.): SEK ~39k + 74k = ~113k  (vs. penny budget ~103k)

Usage:
    python close_ibkr_margin_positions.py           # dry-run, shows plan only
    python close_ibkr_margin_positions.py --execute # places real paper orders

NOTE: Claude never runs --execute; this script is run by the user.
"""
from __future__ import annotations

import argparse
import sys
import os
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)

from ibkr_module import ibkr_client as ic
from ibkr_module import ibkr_state as st

PAPER_PORT   = 4002
ACCOUNT_ID   = "DUR952126"
CLIENT_ID    = 98   # distinct from all other runners

# (symbol, qty, strategy, db_id)
TARGETS = [
    ("ZS",   9, "scorer_portfolio",  427),
    ("ZS",   9, "US Momentum",       428),
    ("ZS",   9, "US Ensemble",       429),
    ("ILMN", 7, "US Momentum",       430),
]


def _dry_run() -> None:
    SEK_PER_USD = 10.35
    print("\n[DRY RUN] Would close the following positions:")
    print(f"{'Symbol':<7} {'Qty':>4} {'Strategy':<25} {'DB_ID':>6}  {'Frees ~SEK'}")
    print("-" * 65)
    total = 0.0
    for sym, qty, strat, db_id in TARGETS:
        notional_sek = qty * (194.71 if sym == "ZS" else 272.60) * SEK_PER_USD
        total += notional_sek
        print(f"{sym:<7} {qty:>4} {strat:<25} {db_id:>6}  ~SEK {notional_sek:,.0f}")
    print(f"\nTotal margin freed: ~SEK {total:,.0f}")
    print(f"Current buying power: SEK 39,087  ->  after: ~SEK {39087+total:,.0f}")
    print(f"\nRun with --execute to place the orders.")


def _execute() -> None:
    print(f"\n[EXECUTE] Connecting to IBKR paper account {ACCOUNT_ID} on port {PAPER_PORT}...")
    ib = ic.connect("127.0.0.1", PAPER_PORT, client_id=CLIENT_ID)
    try:
        errors = []
        for sym, qty, strat, db_id in TARGETS:
            print(f"\n  SELL {qty} {sym}  [{strat}]  db_id={db_id}")
            try:
                trade = ic.place_market_order(ib, ACCOUNT_ID, sym, "SELL", qty)
                print(f"    Order placed: {trade}")

                # Wait up to 15s for fill
                deadline = time.time() + 15
                filled = False
                while time.time() < deadline:
                    ib.sleep(1)
                    t = ib.trades()
                    for tr in t:
                        if tr.order.orderId == trade.order.orderId:
                            if tr.orderStatus.status in ("Filled", "Submitted"):
                                filled = True
                                fill_px = tr.orderStatus.avgFillPrice or 0
                                print(f"    -> {tr.orderStatus.status}  fill_px={fill_px:.2f}")
                                break
                    if filled:
                        break

                # Update DB regardless (market is closed on weekends, will paper-fill at open)
                st.close_buy_position(sym, strat)
                print(f"    DB updated: {sym} [{strat}] -> SOLD")

            except Exception as e:
                print(f"    ERROR: {e}")
                errors.append(f"{sym} [{strat}]: {e}")

        if errors:
            print(f"\n[WARN] Errors encountered:")
            for e in errors:
                print(f"  - {e}")
        else:
            print(f"\n[OK] All {len(TARGETS)} positions closed.")

    finally:
        ib.disconnect()
        print("Disconnected from IBKR.")

    print("\nRun 'python run_ibkr_stocks.py --info' to verify new buying power.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Close margin-freeing positions in IBKR paper account")
    parser.add_argument("--execute", action="store_true",
                        help="Actually place the sell orders (default: dry-run only)")
    args = parser.parse_args()

    if args.execute:
        _execute()
    else:
        _dry_run()


if __name__ == "__main__":
    main()
