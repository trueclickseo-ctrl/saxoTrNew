"""
lookup_fx_uics.py  —  Confirm Saxo SIM UICs for FX cross pairs
--------------------------------------------------------------
Usage:
    python lookup_fx_uics.py
"""

import json, os, sys, time
import requests

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token.json")
SIM_BASE   = "https://gateway.saxobank.com/sim/openapi/"

TARGETS = [
    # Already in universe — re-verify
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "NZDUSD", "USDCHF",
    "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY",
    # EUR crosses
    "EURAUD", "EURNZD", "EURCAD", "EURCHF",
    # GBP crosses
    "GBPAUD", "GBPCAD", "GBPCHF", "GBPNZD",
    # AUD / NZD crosses
    "AUDCAD", "AUDCHF", "AUDNZD",
    "NZDJPY", "NZDCAD", "NZDCHF",
    # CHF / JPY remaining
    "CHFJPY",
]


def load_token():
    try:
        d = json.load(open(TOKEN_FILE))
        if time.time() > float(d.get("obtained_at", 0)) + int(d.get("expires_in", 1200)) - 60:
            print("Token expired — run: python set_token.py")
            sys.exit(1)
        return d["access_token"]
    except FileNotFoundError:
        print("saxo_token.json not found — run: python set_token.py")
        sys.exit(1)


def search_instrument(token, keyword):
    r = requests.get(
        SIM_BASE + "ref/v1/instruments",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "Keywords":      keyword,
            "AssetTypes":    "FxSpot",
            "IncludeNonTradable": False,
        },
        timeout=10,
    )
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}"
    data = r.json().get("Data", [])
    # Find exact symbol match
    for item in data:
        if item.get("Symbol") == keyword:
            return item.get("Identifier"), None
    # Fallback: return first result
    if data:
        first = data[0]
        return first.get("Identifier"), f"(closest match: {first.get('Symbol')})"
    return None, "not found"


def verify_price(token, uic):
    """Confirm UIC is tradeable by fetching a live price."""
    r = requests.get(
        SIM_BASE + "trade/v1/infoprices",
        headers={"Authorization": f"Bearer {token}"},
        params={"Uic": uic, "AssetType": "FxSpot", "FieldGroups": "Quote"},
        timeout=5,
    )
    if r.status_code != 200:
        return None
    q = r.json().get("Quote", {})
    mid = q.get("Mid")
    if mid is None and q.get("Ask") and q.get("Bid"):
        mid = (float(q["Ask"]) + float(q["Bid"])) / 2
    return round(float(mid), 5) if mid else None


def main():
    token = load_token()
    print(f"\n{'Symbol':<10} {'UIC':>8}   {'Mid Price':>12}   Note")
    print("-" * 55)

    results = {}
    for sym in TARGETS:
        uic, note = search_instrument(token, sym)
        if uic is None:
            print(f"{sym:<10} {'—':>8}   {'—':>12}   {note}")
            continue
        price = verify_price(token, uic)
        price_s = f"{price:.5f}" if price else "price N/A"
        print(f"{sym:<10} {uic:>8}   {price_s:>12}   {note or 'OK'}")
        results[sym] = {"uic": uic, "price": price}

    print("\nPaste output above to confirm UICs before adding to universe.py")


if __name__ == "__main__":
    main()
