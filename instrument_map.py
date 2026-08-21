"""
instrument_map.py
------------------
Loads data/instrument_map.csv (Saxo) or data/instrument_map_ibkr.csv (IBKR)
— the mapping between Yahoo Finance tickers (used for backtesting/signal
data) and the broker's own instrument id (Saxo's Uic, or IBKR's conId —
both stored under the "uic" CSV column, same as every other broker call
site in this codebase treats the field). Built by lookup_instruments.py /
lookup_instruments_ibkr.py respectively.

Also carries each instrument's trading currency straight through from the
CSV (both lookup scripts already record it) — callers need this to convert
prices into SEK before comparing them against SEK-denominated cash/risk
figures. A ticker with no currency recorded is skipped rather than
silently assumed to be SEK, since a wrong assumption here is exactly the
bug that caused oversized live orders.
"""

import csv
import os

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
MAP_FILES = {
    "saxo": os.path.join(_DATA_DIR, "instrument_map.csv"),
    "ibkr": os.path.join(_DATA_DIR, "instrument_map_ibkr.csv"),
}
MAP_FILE = MAP_FILES["saxo"]   # back-compat for anything importing the constant directly


def load_instrument_map(broker: str = "saxo") -> dict:
    """Returns {yahoo_ticker: {'uic': int, 'symbol': str, 'currency': str}}
    for every mapped ticker. broker="ibkr" loads instrument_map_ibkr.csv
    (conIds) instead of the Saxo Uic map."""
    map_file = MAP_FILES[broker]
    lookup_script = "lookup_instruments.py" if broker == "saxo" else "lookup_instruments_ibkr.py"
    if not os.path.exists(map_file):
        raise FileNotFoundError(
            f"{map_file} not found. Run {lookup_script} first to build it."
        )
    mapping = {}
    with open(map_file) as f:
        for row in csv.DictReader(f):
            if not row.get("uic"):
                continue  # skip tickers flagged needs_review / unmapped
            currency = (row.get("currency") or "").strip().upper()
            if not currency:
                print(f"  [WARN] {row['yahoo_ticker']}: no currency recorded in "
                      f"{os.path.basename(map_file)} — skipping rather than guessing. "
                      f"Rerun {lookup_script} to fill it in.")
                continue
            mapping[row["yahoo_ticker"]] = {
                "uic": int(float(row["uic"])),
                "symbol": row["symbol"],
                "currency": currency,
            }
    return mapping
