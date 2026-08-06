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
"""

import csv
import os

from atos.logger import get_logger
logger = get_logger("instrument_map")

MAP_FILE = os.path.join(os.path.dirname(__file__), "data", "instrument_map.csv")


def load_instrument_map() -> dict:
    """Returns {yahoo_ticker: {'uic': int, 'symbol': str, 'currency': str}}
    for every mapped ticker."""
    if not os.path.exists(MAP_FILE):
        raise FileNotFoundError(
            f"{MAP_FILE} not found. Run lookup_instruments.py first to build it."
        )
    mapping = {}
    with open(MAP_FILE) as f:
        for row in csv.DictReader(f):
            if not row.get("uic"):
                continue  # skip tickers flagged needs_review / unmapped
            currency = (row.get("currency") or "").strip().upper()
            if not currency:
                logger.warning("%s: no currency in instrument_map.csv — skipping. "
                               "Rerun lookup_instruments.py to fill it in.", row['yahoo_ticker'])
                continue
            mapping[row["yahoo_ticker"]] = {
                "uic": int(row["uic"]),
                "symbol": row["symbol"],
                "currency": currency,
            }
    logger.info("Instrument map loaded: %d instruments mapped", len(mapping))
    return mapping
