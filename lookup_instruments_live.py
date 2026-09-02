"""
lookup_instruments_live.py
--------------------------
One-shot, OPERATOR-RUN: builds data/instrument_map_live.csv -- the Yahoo-ticker
-> Saxo LIVE Uic mapping for the real-money US Blend sleeve (atos_live_stocks.py).

Why a separate file from lookup_instruments.py / instrument_map.csv:
Saxo assigns DIFFERENT Uics on SIM vs LIVE for the same instrument
(saxo_client.find_instrument docstring). Reusing a SIM Uic for a LIVE order can
hit a completely unrelated instrument. This re-derives every Blend-eligible
ticker's Uic against env="live".

Scope: US Blend trades US names only, so this only looks up atos.universe.US_TICKERS
and only keeps matches that are CurrencyCode == "USD" on a US exchange. A ticker
with no USD/US match is written with an empty uic + needs_review note and is then
EXCLUDED from the LIVE Blend target set by instrument_map.load_instrument_map()
(never guessed).

Read-only against the LIVE ref-data endpoint -- places no orders -- but it DOES
hit the live host, so it is an operator action (Claude never runs it).

Usage:
    python lookup_instruments_live.py            # full run (~500 tickers, ~9 min)
    python lookup_instruments_live.py --limit 20 # smoke test
"""

import argparse
import csv
import os
import time

from atos.universe import US_TICKERS
from saxo_client import find_instrument

OUT_FILE = os.path.join(os.path.dirname(__file__), "data", "instrument_map_live.csv")

# Saxo ExchangeId values that are US venues trading in USD.
US_EXCHANGE_IDS = {"NASDAQ", "NYSE", "NYSE_ARCA", "ARCA", "BATS", "AMEX", "NYSE_MKT", "PINK"}


def _pick_us_usd(matches: list[dict]) -> dict | None:
    """The best USD/US-exchange match, or None."""
    usd = [m for m in matches if (m.get("CurrencyCode") or "").upper() == "USD"]
    if not usd:
        return None
    on_us = [m for m in usd if (m.get("ExchangeId") or "").upper() in US_EXCHANGE_IDS]
    pool = on_us or usd
    # Prefer an exact symbol match (Saxo Symbol is like "AAPL:xnas").
    return pool[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only look up the first N tickers (smoke test)")
    args = ap.parse_args()

    tickers = list(dict.fromkeys(US_TICKERS))
    if args.limit:
        tickers = tickers[: args.limit]

    rows = []
    for i, ticker in enumerate(tickers, 1):
        # US_TICKERS are already plain (no Yahoo suffix) for US names.
        search_term = ticker.split("-")[0] if "-" in ticker else ticker
        try:
            matches = find_instrument(search_term, "Stock", env="live")
        except Exception as e:
            print(f"  [{i}/{len(tickers)}] {ticker}: lookup ERROR {e}")
            rows.append({"yahoo_ticker": ticker, "uic": "", "symbol": "", "exchange": "",
                         "currency": "", "needs_review": f"yes - lookup error: {e}"})
            time.sleep(1.5)
            continue

        best = _pick_us_usd(matches)
        if not best:
            note = "yes - no USD/US match" if matches else "yes - not found"
            print(f"  [{i}/{len(tickers)}] {ticker}: {note}")
            rows.append({"yahoo_ticker": ticker, "uic": "", "symbol": "", "exchange": "",
                         "currency": "", "needs_review": note})
            time.sleep(1)
            continue

        rows.append({
            "yahoo_ticker": ticker,
            "uic": best.get("Identifier"),
            "symbol": best.get("Symbol"),
            "exchange": best.get("ExchangeId"),
            "currency": best.get("CurrencyCode"),
            "needs_review": "",
        })
        print(f"  [{i}/{len(tickers)}] {ticker} -> LIVE Uic {best.get('Identifier')} "
              f"({best.get('Symbol')}, {best.get('ExchangeId')})")
        time.sleep(1)

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["yahoo_ticker", "uic", "symbol", "exchange",
                                          "currency", "needs_review"])
        w.writeheader()
        w.writerows(rows)

    mapped = sum(1 for r in rows if r["uic"])
    print(f"\nSaved: {OUT_FILE}  ({mapped}/{len(rows)} mapped)")
    print("IMPORTANT: manually diff each LIVE Uic against data/instrument_map.csv")
    print("(the SIM Uic) and spot-check a few in SaxoTraderGO before Phase 2.")


if __name__ == "__main__":
    main()
