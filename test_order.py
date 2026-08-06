"""
test_order.py
--------------
Places ONE small test order on your SIM account (1 share, market order) to
prove the full pipeline works: auth -> instrument lookup -> order placement
-> position confirmation. No real money is involved (SIM environment only).

This is a manual, one-time test — the real bot will do this automatically
based on strategy signals, but we want to see it work end-to-end by hand
first.
"""

import csv
import os
from saxo_client import place_market_order, get_positions

# This script places a REAL (simulated) order and bypasses the ATOS risk
# engine, so gate it behind the same kill switch as the bot, and require an
# explicit confirmation so it can never fire an order unattended.
KILL_SWITCH_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "STOP_TRADING")

# Pick one instrument from your verified instrument_map.csv to test with.
# SAND.ST (Sandvik) is used here as a stable, liquid example — change if
# you'd prefer to test with a different one.
TEST_TICKER = "SAND.ST"


def load_uic_for_ticker(ticker: str) -> tuple[int, str]:
    with open("data/instrument_map.csv") as f:
        for row in csv.DictReader(f):
            if row["yahoo_ticker"] == ticker:
                if not row["uic"]:
                    raise ValueError(f"{ticker} has no Uic in instrument_map.csv — pick a different ticker.")
                return int(row["uic"]), row["symbol"]
    raise ValueError(f"{ticker} not found in data/instrument_map.csv")


def main():
    if os.path.exists(KILL_SWITCH_FILE):
        print("STOP_TRADING file present — kill switch active. No order placed.")
        return

    uic, symbol = load_uic_for_ticker(TEST_TICKER)
    print(f"Testing with {TEST_TICKER} (Uic {uic}, Saxo symbol {symbol})")

    confirm = input(f"Place a REAL SIM BUY order for 1 share of {TEST_TICKER}? [y/N] ")
    if confirm.strip().lower() != "y":
        print("Aborted — no order placed.")
        return

    print("\nPlacing a BUY order for 1 share (market order)...")
    result = place_market_order(uic=uic, asset_type="Stock", buy_sell="Buy", amount=1)
    print("Order response:", result)

    print("\nFetching positions to confirm...")
    positions = get_positions()
    for pos in positions.get("Data", []):
        base = pos.get("PositionBase", {})
        if base.get("Uic") == uic:
            print(f"  CONFIRMED: {base.get('Amount')} share(s) of Uic {uic} now held.")
            print(f"  Entry price: {base.get('OpenPrice')} {pos.get('PositionView', {}).get('CurrentPrice', '')}")
            return

    print("  Position not found yet — it may take a moment on SIM. Check again with:")
    print("  python -c \"from saxo_client import get_positions; print(get_positions())\"")


if __name__ == "__main__":
    main()