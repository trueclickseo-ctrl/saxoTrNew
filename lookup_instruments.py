"""
lookup_instruments.py
----------------------
One-time step: converts your OMX30 (or OMXC25/US) ticker list into Saxo's
internal Uic codes, since orders can't be placed with plain ticker symbols.

Run this once (after saxo_client.py connection test succeeds). It saves
the mapping to data/instrument_map.csv so we only ever have to do this once
per ticker.
"""

import csv
import time
import config
from saxo_client import find_instrument

# Maps our Yahoo-style ticker suffixes to Saxo's exchange codes, so we can
# pick the RIGHT match when a company has listings on multiple exchanges.
EXCHANGE_HINT = {
    ".ST": "OMX",       # Stockholm
    ".CO": "CSE",        # Copenhagen
    ".DE": "GER",         # Germany/Xetra
    ".L": "LSE",           # London
    ".PA": "PAR",           # Paris
    ".AS": "AMS",            # Amsterdam
    ".SW": "SWX",             # Swiss (SIX)
    ".TO": "TOR",              # Toronto
    ".T": "TSE",                # Tokyo
}


# Saxo's ExchangeId codes for the exchanges we care about
EXCHANGE_ID_FOR_HINT = {
    "OMX": "SSE",       # Stockholm
    "CSE": "CSE",       # Copenhagen
    "NASDAQ": "NASDAQ", # US Nasdaq
    "GER": "GER",        # Germany/Xetra — best-guess code, verify
    "LSE": "LSE",         # London — best-guess code, verify
    "PAR": "PAR",          # Paris — LOW CONFIDENCE guess, verify closely
    "AMS": "AMS",           # Amsterdam — LOW CONFIDENCE guess, verify closely
    "SWX": "SWX",            # Swiss — LOW CONFIDENCE guess, verify closely
    "TOR": "TOR",             # Toronto — LOW CONFIDENCE guess, verify closely
    "TSE": "TSE",              # Tokyo — LOW CONFIDENCE guess, verify closely
}


def strip_suffix(ticker: str) -> tuple[str, str]:
    for suffix, exch_hint in EXCHANGE_HINT.items():
        if ticker.endswith(suffix):
            return ticker[: -len(suffix)], exch_hint
    # No suffix — if it's one of our known Nasdaq-100 tickers, hint the
    # exchange explicitly rather than leaving it unfiltered. Common tickers
    # like well-known mega-caps rarely collide across exchanges, but this
    # costs nothing and catches the rare case where they do.
    if ticker in config.NASDAQ100_TICKERS:
        return ticker, "NASDAQ"
    return ticker, None


def main():
    rows = []
    for ticker in config.ACTIVE_UNIVERSE:
        search_term, exch_hint = strip_suffix(ticker)

        matches = find_instrument(search_term)

        # Fallback: Saxo's search sometimes fails on "ERIC-B" but succeeds on
        # just "ERIC" (the share-class letter after the dash trips it up)
        used_term = search_term
        if not matches and "-" in search_term:
            root = search_term.split("-")[0]
            matches = find_instrument(root)
            used_term = root
            if matches:
                print(f"    (fell back to '{root}' for {ticker})")

        if not matches:
            print(f"  NOT FOUND: {ticker} (searched '{search_term}' and fallback)")
            rows.append({"yahoo_ticker": ticker, "uic": "", "symbol": "", "exchange": "", "currency": "", "needs_review": "yes - not found"})
            time.sleep(1)
            continue

        # CRITICAL: filter to the expected exchange first. Without this,
        # keyword search can return a same-name/similar-symbol company on a
        # totally different exchange (e.g. AstraZeneca on London instead of
        # Stockholm, or even an unrelated company like "NDAQ" for "NDA").
        expected_exchange = EXCHANGE_ID_FOR_HINT.get(exch_hint)
        exchange_matches = [m for m in matches if m.get("ExchangeId") == expected_exchange] if expected_exchange else matches

        needs_review = ""
        if expected_exchange and not exchange_matches:
            # Nothing on the expected exchange at all — this result is unverified
            needs_review = f"yes - no match on {expected_exchange}, showing best guess"
            exchange_matches = matches

        candidates = exchange_matches

        # Prefer a match whose Symbol contains the share-class letter we're
        # after (e.g. "B" from "ERIC-B") if the search fell back to the root
        best = candidates[0]
        if used_term != search_term and "-" in search_term:
            class_letter = search_term.split("-")[1][0].upper()  # e.g. "B" from "ERIC-B"
            for m in candidates:
                sym = (m.get("Symbol") or "").upper()
                if f"{class_letter}:" in sym or sym.endswith(class_letter) or f" {class_letter}" in sym:
                    best = m
                    break

        rows.append({
            "yahoo_ticker": ticker,
            "uic": best.get("Identifier"),
            "symbol": best.get("Symbol"),
            "exchange": best.get("ExchangeId"),
            "currency": best.get("CurrencyCode"),
            "needs_review": needs_review,
        })
        flag = "  <-- REVIEW THIS" if needs_review else ""
        print(f"  {ticker} -> Uic {best.get('Identifier')} ({best.get('Symbol')}, {best.get('ExchangeId')}){flag}")
        time.sleep(1)  # be gentle with the API

    with open("data/instrument_map.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["yahoo_ticker", "uic", "symbol", "exchange", "currency", "needs_review"])
        writer.writeheader()
        writer.writerows(rows)

    print("\nSaved: data/instrument_map.csv")
    print("IMPORTANT: open this file and manually verify each match before")
    print("we use it for real order placement — automated symbol search can")
    print("occasionally pick the wrong exchange listing for the same company.")


if __name__ == "__main__":
    main()