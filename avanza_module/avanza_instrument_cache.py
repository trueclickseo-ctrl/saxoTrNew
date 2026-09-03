"""
avanza_instrument_cache.py
--------------------------
Maps US stock tickers (e.g. "AAPL") to Avanza order_book_ids.

Cache file: data/avanza_instrument_cache.json
Format:     {ticker: {id, name, currency, country, market, last_updated}}

Usage:
    cache = load_cache()
    ob_id = lookup(client, "AAPL", cache)   # searches Avanza if not cached
    save_cache(cache)
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from avanza import Avanza

_ROOT      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_ROOT, "data", "avanza_instrument_cache.json")

# Tickers that failed search — skip them next time (avoids repeated API calls)
_NOT_FOUND_SENTINEL = "__NOT_FOUND__"


def load_cache() -> dict:
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    tmp = _CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, _CACHE_FILE)


def _score_hit(h: dict, ticker: str) -> int:
    """Higher is better. Prefer US-listed, exact ticker match."""
    score = 0
    h_ticker  = (h.get("ticker") or "").upper()
    h_country = (h.get("country") or "").upper()
    h_currency= (h.get("currency") or "").upper()
    h_market  = (h.get("market") or "").upper()

    if h_ticker == ticker.upper():
        score += 100
    if h_country in ("US", "USA"):
        score += 50
    if h_currency == "USD":
        score += 30
    if any(m in h_market for m in ("NYSE", "NASDAQ", "ARCX", "XNAS", "XNYS")):
        score += 20
    return score


def lookup(client: "Avanza", ticker: str, cache: dict,
           force_refresh: bool = False) -> str | None:
    """Return the Avanza order_book_id for a ticker, searching if not cached.

    Returns None if the ticker cannot be found on Avanza.
    """
    from avanza_module.avanza_client import search_stocks

    if not force_refresh and ticker in cache:
        entry = cache[ticker]
        if isinstance(entry, dict):
            if entry.get("id") == _NOT_FOUND_SENTINEL:
                return None
            return entry.get("id")

    hits = search_stocks(client, ticker, limit=15)

    if not hits:
        cache[ticker] = {"id": _NOT_FOUND_SENTINEL, "last_updated": datetime.now().isoformat()}
        return None

    # Score and pick best match
    scored = sorted(hits, key=lambda h: _score_hit(h, ticker), reverse=True)
    best = scored[0]

    if not best.get("id"):
        cache[ticker] = {"id": _NOT_FOUND_SENTINEL, "last_updated": datetime.now().isoformat()}
        return None

    cache[ticker] = {
        "id":           best["id"],
        "name":         best.get("name", ""),
        "ticker":       best.get("ticker", ticker),
        "currency":     best.get("currency", "USD"),
        "country":      best.get("country", "US"),
        "market":       best.get("market", ""),
        "last_updated": datetime.now().isoformat(),
    }
    return best["id"]


def bulk_lookup(client: "Avanza", tickers: list[str],
                cache: dict | None = None,
                verbose: bool = True) -> dict[str, str | None]:
    """Resolve a list of tickers to order_book_ids. Updates cache in-place.

    Returns {ticker: order_book_id | None}
    """
    if cache is None:
        cache = load_cache()

    result = {}
    for ticker in tickers:
        ob_id = lookup(client, ticker, cache)
        result[ticker] = ob_id
        if verbose:
            status = ob_id if ob_id else "NOT FOUND"
            print(f"  {ticker:8s} → {status}")

    save_cache(cache)
    return result
