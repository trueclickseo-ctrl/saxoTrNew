"""
close_eu_positions.py
---------------------
Closes all EU and OMX30 open positions from Saxo SIM and removes them from
the ATOS database. Run this manually once to exit non-US holdings.

Usage:
    python close_eu_positions.py
"""

import os
import sys
import json
from datetime import datetime, date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import saxo_client
from atos import database as db

# Positions to close: (symbol-as-in-DB, uic, shares, asset_type)
CLOSE_TARGETS = [
    ("PRX",    14838059, 1,  "Stock"),   # Prosus NV  — AMS/EU
    ("NIBE-B", 1858,     26, "Stock"),   # Nibe Industrier B — OMX30
    ("HEXA-B", 1774,     11, "Stock"),   # Hexagon AB Ser. B  — OMX30
    ("HM-B",   110,      12, "Stock"),   # Hennes & Mauritz B — OMX30
]

# -- Load token --------------------------------------------------------
token_file = os.path.join(BASE_DIR, "saxo_token.json")
if os.path.exists(token_file):
    tok = json.load(open(token_file)).get("access_token")
    if tok:
        os.environ["SAXO_TOKEN"] = tok

# -- Fetch live Saxo positions so we use actual share counts ----------
try:
    from atos_dashboard import _load_saxo_token, _saxo_get_positions
    live_tok = _load_saxo_token()
    live_positions = _saxo_get_positions(live_tok) if live_tok else []
    # Build dict: symbol -> Amount
    live_shares = {}
    for p in live_positions:
        sym = p.get("DisplayAndFormat", {}).get("Symbol", "")
        amt = p.get("PositionBase", {}).get("Amount", 0)
        live_shares[sym] = amt
    print(f"Live Saxo positions loaded: {len(live_shares)} positions")
except Exception as e:
    print(f"[WARN] Could not load live positions ({e}) — using hardcoded share counts")
    live_shares = {}

# -- Close each position -----------------------------------------------
open_trades = db.get_open_trades()
db_by_ticker = {t["ticker"]: t for t in open_trades}

print()
for ticker, uic, default_shares, asset_type in CLOSE_TARGETS:
    # Prefer live Saxo share count; fall back to hardcoded
    sym_in_saxo = None
    for sym in live_shares:
        if ticker.upper().replace("-", "") in sym.upper().replace("_", "").replace("-", ""):
            sym_in_saxo = sym
            break
    shares = live_shares.get(sym_in_saxo, default_shares) if sym_in_saxo else default_shares

    print(f"  Selling {shares} × {ticker} (UIC {uic}) ...", end=" ", flush=True)
    try:
        saxo_client.place_market_order(uic, asset_type, "Sell", int(shares))
        print("OK")
    except Exception as e:
        print(f"FAILED: {e}")
        continue

    # Close in ATOS DB if present
    db_ticker_keys = [t for t in db_by_ticker if ticker.upper().replace("-","") in t.upper().replace("-","").replace("_","")]
    for db_key in db_ticker_keys:
        trade = db_by_ticker[db_key]
        db.close_trade(trade["id"], trade.get("entry_price", 0), "manual_close", 0.0, 0.0)
        print(f"    → DB trade #{trade['id']} closed for {db_key}")

print()
print("Done. Refresh the dashboard to confirm positions are gone.")
