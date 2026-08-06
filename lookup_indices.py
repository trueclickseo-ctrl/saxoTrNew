"""
lookup_indices.py
------------------
One-time step: finds Saxo's Uic codes for the index CFDs you want to trade
(DAX, Copenhagen 25, and the three US indices), and saves them to
data/index_map.csv. Same idea as lookup_instruments.py, but for indices
instead of single stocks — indices are looked up by Saxo's own display
name rather than a ticker+exchange combination, and use AssetType
'CfdOnIndex' instead of 'Stock'.

Run this after saxo_client.py's connection test succeeds (needs a valid
token/AccountKey already set up, same as everything else in this project).

IMPORTANT: same as with the stock lookup — a keyword search can occasionally
surface the wrong instrument (e.g. a Nasdaq-listed stock CFD instead of the
Nasdaq index CFD). ALWAYS open the resulting CSV and eyeball each row before
this is used for real order placement.
"""

import csv
import time
from saxo_client import find_instrument

# Our own label -> the search keyword we'll send Saxo, and what its
# CurrencyCode SHOULD be (used only as a sanity check/flag, not a filter,
# since a wrong currency here is a strong sign of a wrong match).
INDEX_TARGETS = [
    {"label": "DAX_DE", "keywords": ["Germany 40", "GER 40", "DAX"], "expected_currency": "EUR"},
    {"label": "COPENHAGEN_25", "keywords": ["Denmark 25", "OMXC25", "OMXC 25"], "expected_currency": "DKK"},
    {"label": "US_SP500", "keywords": ["US 500", "S&P 500"], "expected_currency": "USD"},
    {"label": "US_NASDAQ100", "keywords": ["US Tech 100", "NAS 100", "Nasdaq 100"], "expected_currency": "USD"},
    {"label": "US_DOW30", "keywords": ["US 30", "Wall Street 30", "Dow Jones"], "expected_currency": "USD"},
]


def search_with_fallback(keywords: list[str]) -> tuple[list[dict], str]:
    """Tries each keyword variant in turn until one returns results."""
    for kw in keywords:
        matches = find_instrument(kw, asset_type="CfdOnIndex")
        if matches:
            return matches, kw
    return [], keywords[0]


def main():
    rows = []
    for target in INDEX_TARGETS:
        matches, used_keyword = search_with_fallback(target["keywords"])

        if not matches:
            print(f"  NOT FOUND: {target['label']} (tried {target['keywords']})")
            rows.append({
                "label": target["label"], "uic": "", "symbol": "", "exchange": "",
                "currency": "", "needs_review": "yes - not found, try searching manually in Explorer",
            })
            time.sleep(1)
            continue

        best = matches[0]
        needs_review = ""
        if best.get("CurrencyCode") != target["expected_currency"]:
            needs_review = (f"yes - expected currency {target['expected_currency']}, "
                             f"got {best.get('CurrencyCode')} - verify this is the right instrument")
        if len(matches) > 1:
            needs_review = (needs_review + " | " if needs_review else "") + \
                            f"yes - {len(matches)} matches found for '{used_keyword}', confirm this is the right one"

        rows.append({
            "label": target["label"],
            "uic": best.get("Identifier"),
            "symbol": best.get("Symbol"),
            "exchange": best.get("ExchangeId"),
            "currency": best.get("CurrencyCode"),
            "needs_review": needs_review,
        })
        flag = "  <-- REVIEW THIS" if needs_review else ""
        print(f"  {target['label']} -> Uic {best.get('Identifier')} "
              f"({best.get('Symbol')}, {best.get('ExchangeId')}, {best.get('CurrencyCode')}){flag}")
        time.sleep(1)  # be gentle with the API

    with open("data/index_map.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "uic", "symbol", "exchange", "currency", "needs_review"])
        writer.writeheader()
        writer.writerows(rows)

    print("\nSaved: data/index_map.csv")
    print("IMPORTANT: open this file and manually verify each row (especially any")
    print("flagged 'needs_review') before we wire these into live/paper trading.")


if __name__ == "__main__":
    main()
