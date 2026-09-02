"""
instrument_map.py
------------------
Loads data/instrument_map.csv — the mapping between Yahoo Finance tickers
(used for backtesting/signal data) and Saxo's internal Uic codes (needed to
place orders). Built by lookup_instruments.py.

Also carries each instrument's trading currency straight through from the
CSV (lookup_instruments.py already records it, via Saxo's CurrencyCode) —
callers need this to convert prices into SEK before comparing them against
SEK-denominated cash/risk figures. A ticker with no currency recorded is
skipped rather than silently assumed to be SEK, since a wrong assumption
here is exactly the bug that caused oversized live orders.

LIVE stocks: the real-money US Blend sleeve (atos_live_stocks.py) passes
path=MAP_FILE_LIVE + require_usd=True so it loads data/instrument_map_live.csv
(Saxo LIVE Uics, re-derived by lookup_instruments_live.py — SIM and LIVE Uics
differ) and drops any ticker that isn't a mapped USD instrument.
"""

import csv
import os

MAP_FILE = os.path.join(os.path.dirname(__file__), "data", "instrument_map.csv")
MAP_FILE_LIVE = os.path.join(os.path.dirname(__file__), "data", "instrument_map_live.csv")


def load_instrument_map(path: str | None = None, require_usd: bool = False) -> dict:
    """Returns {yahoo_ticker: {'uic': int, 'symbol': str, 'currency': str}}
    for every mapped ticker.

    path:        which CSV to read (default: the SIM map, MAP_FILE).
    require_usd: drop any ticker whose currency isn't USD (LIVE US Blend —
                 it only ever trades US names; a non-USD row is a bad match).
    """
    map_file = path or MAP_FILE
    if not os.path.exists(map_file):
        raise FileNotFoundError(
            f"{map_file} not found. Run "
            f"{'lookup_instruments_live.py' if map_file == MAP_FILE_LIVE else 'lookup_instruments.py'}"
            f" first to build it."
        )
    mapping = {}
    with open(map_file, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("uic"):
                continue  # skip tickers flagged needs_review / unmapped
            currency = (row.get("currency") or "").strip().upper()
            if not currency:
                print(f"  [WARN] {row['yahoo_ticker']}: no currency recorded in "
                      f"{os.path.basename(map_file)} — skipping rather than guessing.")
                continue
            if require_usd and currency != "USD":
                print(f"  [WARN] {row['yahoo_ticker']}: currency {currency} != USD in "
                      f"{os.path.basename(map_file)} — excluded from the LIVE Blend set.")
                continue
            mapping[row["yahoo_ticker"]] = {
                "uic": int(float(row["uic"])),
                "symbol": row["symbol"],
                "currency": currency,
            }
    return mapping
