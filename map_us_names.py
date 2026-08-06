"""
map_us_names.py
----------------
Targeted, APPEND-ONLY UIC lookup for the US tickers in the ATOS universe that
are not yet in data/instrument_map.csv. Unlike lookup_instruments.py this does
NOT rebuild the whole file from the legacy config universe — it preserves every
existing row and only appends missing US names, so OMX30/CPH25 mappings are safe.

Selection rule for a US name: the match whose Symbol root == ticker exactly,
on NYSE or NASDAQ, in USD, AssetType Stock (the primary common share). Anything
ambiguous is written with a blank uic + needs_review flag rather than guessed.
"""

import csv
import os
import time

import saxo_client
from atos.universe import US_TICKERS

MAP_FILE = os.path.join(os.path.dirname(__file__), "data", "instrument_map.csv")
US_EXCHANGES = {"NYSE", "NASDAQ"}
FIELDS = ["yahoo_ticker", "uic", "symbol", "exchange", "currency", "needs_review"]


def pick_us_primary(ticker: str, matches: list) -> dict | None:
    t = ticker.upper()
    for m in matches:
        root = (m.get("Symbol") or "").split(":")[0].upper()
        if (root == t
                and m.get("ExchangeId") in US_EXCHANGES
                and m.get("CurrencyCode") == "USD"
                and m.get("AssetType") == "Stock"):
            return m
    return None


def main():
    # Load existing rows verbatim.
    with open(MAP_FILE, newline="") as f:
        existing = list(csv.DictReader(f))
    have = {r["yahoo_ticker"] for r in existing}

    missing = [t for t in US_TICKERS if t not in have]
    print(f"US universe: {len(US_TICKERS)} | already mapped: {len(US_TICKERS)-len(missing)} | to look up: {len(missing)}")

    new_rows = []
    for ticker in missing:
        try:
            matches = saxo_client.find_instrument(ticker, "Stock")
        except Exception as e:
            print(f"  {ticker}: API ERROR {type(e).__name__}: {e}")
            new_rows.append({"yahoo_ticker": ticker, "uic": "", "symbol": "",
                             "exchange": "", "currency": "", "needs_review": f"api error: {e}"})
            time.sleep(1)
            continue

        best = pick_us_primary(ticker, matches)
        if best:
            new_rows.append({
                "yahoo_ticker": ticker,
                "uic": best.get("Identifier"),
                "symbol": best.get("Symbol"),
                "exchange": best.get("ExchangeId"),
                "currency": best.get("CurrencyCode"),
                "needs_review": "",
            })
            print(f"  {ticker:6} -> Uic {best.get('Identifier')} ({best.get('Symbol')}, {best.get('ExchangeId')})")
        else:
            new_rows.append({"yahoo_ticker": ticker, "uic": "", "symbol": "",
                             "exchange": "", "currency": "",
                             "needs_review": "no exact US primary (NYSE/NASDAQ/USD) match"})
            print(f"  {ticker:6} -> NO EXACT MATCH  <-- REVIEW")
        time.sleep(1)

    # Append (atomic write of the combined set).
    tmp = f"{MAP_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(existing + new_rows)
    os.replace(tmp, MAP_FILE)

    mapped = sum(1 for r in new_rows if r["uic"])
    print(f"\nAppended {len(new_rows)} rows ({mapped} mapped, {len(new_rows)-mapped} need review).")


if __name__ == "__main__":
    main()
