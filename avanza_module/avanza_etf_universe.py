"""
avanza_etf_universe.py
----------------------
Broad-market UCITS ETF universe for the Avanza ETF strategy.

Each ETF has:
  ticker      - Xetra ticker (Avanza uses DE exchange, not LSE)
  yahoo       - Yahoo Finance ticker for historical momentum data
  name        - display name
  region      - for grouping/display
  avanza_id   - pre-confirmed Avanza orderBookId (from live API search)

Avanza lookup maps ticker -> orderBookId and stores in
data/avanza_etf_cache.json (separate from stock cache).
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from avanza import Avanza

_ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_ROOT, "data", "avanza_etf_cache.json")

# ── Universe definition ───────────────────────────────────────────────────────

# Avanza lists UCITS ETFs under their Xetra (DE) tickers, not LSE tickers.
# All confirmed present via search_for_instrument(EXCHANGE_TRADED_FUND, ...).
UNIVERSE: list[dict] = [
    {"ticker": "EUNL",  "yahoo": "EUNL.DE", "name": "iShares Core MSCI World UCITS",        "region": "Global Developed",  "avanza_id": "384747"},
    {"ticker": "VWCE",  "yahoo": "VWCE.DE", "name": "Vanguard FTSE All-World Acc",           "region": "Global All-World",  "avanza_id": "1063827"},
    {"ticker": "SXR8",  "yahoo": "SXR8.DE", "name": "iShares Core S&P 500 UCITS",            "region": "US Large Cap",      "avanza_id": "1063851"},
    {"ticker": "EQQQ",  "yahoo": "EQQQ.L",  "name": "Invesco EQQQ Nasdaq-100 UCITS",         "region": "US Tech",           "avanza_id": "1064025"},
    {"ticker": "SPYE",  "yahoo": "SPYE.DE", "name": "SPDR MSCI Europe UCITS",                "region": "Europe",            "avanza_id": "1063822"},
    {"ticker": "SPYM",  "yahoo": "SPYM.DE", "name": "SPDR MSCI Emerging Markets UCITS",      "region": "Emerging Markets",  "avanza_id": "1064076"},
    {"ticker": "IUSN",  "yahoo": "IUSN.DE", "name": "iShares MSCI World Small Cap UCITS",    "region": "Global Small Cap",  "avanza_id": "1064169"},
    {"ticker": "ZPDJ",  "yahoo": "ZPDJ.DE", "name": "SPDR MSCI Japan UCITS",                 "region": "Japan",             "avanza_id": "1233019"},
]

TICKERS = [e["ticker"] for e in UNIVERSE]
YAHOO_MAP = {e["ticker"]: e["yahoo"] for e in UNIVERSE}


# ── ETF cache (separate from stock cache) ────────────────────────────────────

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


_NOT_FOUND = "__NOT_FOUND__"


def _score_etf_hit(h: dict, ticker: str) -> int:
    """Score Avanza search hits — prefer ETF/index fund type, exact ticker."""
    score = 0
    h_ticker = (h.get("ticker") or "").upper()
    h_market = (h.get("market") or "").upper()
    h_name   = (h.get("name") or "").lower()

    if h_ticker == ticker.upper():
        score += 100
    # Prefer ETF-like instruments over regular stocks
    if any(x in h_name for x in ("etf", "ucits", "index", "ishares", "vanguard",
                                   "xtrackers", "invesco", "lyxor", "amundi")):
        score += 60
    # Prefer European exchanges
    if any(x in h_market for x in ("LONDON", "LSE", "XETRA", "NASDAQ STOCKHOLM",
                                    "EURONEXT", "AMSTERDAM")):
        score += 30
    return score


def lookup_etf(client: "Avanza", ticker: str, cache: dict,
               force_refresh: bool = False) -> str | None:
    """Return Avanza orderBookId for an ETF ticker. None if not found.

    Priority:
      1. Cache hit (unless force_refresh)
      2. Pre-confirmed avanza_id from UNIVERSE definition
      3. Live search via search_for_instrument(EXCHANGE_TRADED_FUND, ...)
    """
    if not force_refresh and ticker in cache:
        entry = cache[ticker]
        if isinstance(entry, dict):
            if entry.get("id") == _NOT_FOUND:
                return None
            return entry.get("id")

    # Use pre-confirmed avanza_id if present in universe definition
    etf_def = next((e for e in UNIVERSE if e["ticker"].upper() == ticker.upper()), None)
    if etf_def and etf_def.get("avanza_id") and not force_refresh:
        ob_id = etf_def["avanza_id"]
        cache[ticker] = {
            "id":           ob_id,
            "name":         etf_def.get("name", ""),
            "ticker":       ticker,
            "currency":     "EUR",
            "yahoo":        etf_def.get("yahoo", ""),
            "last_updated": datetime.now().isoformat(),
        }
        return ob_id

    # Live search fallback
    try:
        from avanza import InstrumentType
        hits = client.search_for_instrument(InstrumentType.EXCHANGE_TRADED_FUND, ticker, limit=20)
        if isinstance(hits, dict):
            hits = hits.get("hits") or hits.get("results") or []
    except Exception:
        hits = []

    if not hits:
        cache[ticker] = {"id": _NOT_FOUND, "last_updated": datetime.now().isoformat()}
        return None

    scored = sorted(hits, key=lambda h: _score_etf_hit(h, ticker), reverse=True)
    best   = scored[0]

    ob_id = best.get("id") or best.get("orderbookId") or best.get("orderBookId")
    if not ob_id:
        cache[ticker] = {"id": _NOT_FOUND, "last_updated": datetime.now().isoformat()}
        return None

    cache[ticker] = {
        "id":           str(ob_id),
        "name":         best.get("name", ""),
        "ticker":       best.get("ticker", ticker),
        "currency":     best.get("currency", "EUR"),
        "country":      best.get("country", ""),
        "market":       best.get("market", ""),
        "yahoo":        YAHOO_MAP.get(ticker, ""),
        "last_updated": datetime.now().isoformat(),
    }
    return str(ob_id)


def resolve_universe(client: "Avanza", force_refresh: bool = False) -> dict:
    """Resolve all universe tickers to Avanza orderBookIds.

    Returns {ticker: orderBookId | None}.
    Prints a summary table.
    """
    cache = load_cache()
    result = {}
    print(f"\n  Resolving {len(UNIVERSE)} ETF universe tickers against Avanza...")
    print(f"  {'Ticker':8s}  {'OrderBookId':12s}  {'Name':40s}  {'Market'}")
    print("  " + "-" * 80)
    for etf in UNIVERSE:
        t = etf["ticker"]
        ob_id = lookup_etf(client, t, cache, force_refresh=force_refresh)
        entry = cache.get(t, {})
        status = ob_id if ob_id else "NOT FOUND"
        print(f"  {t:8s}  {status:12s}  {entry.get('name','')[:40]:40s}  {entry.get('market','')}")
        result[t] = ob_id
    save_cache(cache)
    found = sum(1 for v in result.values() if v)
    print(f"\n  {found}/{len(UNIVERSE)} ETFs found on Avanza.")
    return result
