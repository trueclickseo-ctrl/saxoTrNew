"""
futures/universe.py
-------------------
Market definitions for the futures trend-following strategy.

Saxo SIM uses CfdOnFutures — CFDs that track futures prices continuously with
no expiry/roll management needed.  UICs are auto-discovered via the Saxo search
API and cached in data/futures_uic_cache.json so we don't hit the API every run.

Run `python futures/runner.py --discover` to (re)populate the cache.
"""

import json
import logging
import os

logger = logging.getLogger("futures.universe")

_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR  = os.path.join(_ROOT, "data")
UIC_CACHE = os.path.join(DATA_DIR, "futures_uic_cache.json")

# ── Market definitions ────────────────────────────────────────────────────
# yf_ticker is used for backtesting; search_key drives Saxo instrument search.
MARKETS = [
    {
        "symbol":      "ES",
        "description": "E-mini S&P 500",
        "yf_ticker":   "ES=F",
        "search_key":  "S&P 500 E-Mini",
        "currency":    "USD",
    },
    {
        "symbol":      "GC",
        "description": "Gold",
        "yf_ticker":   "GC=F",
        "search_key":  "Gold",
        "currency":    "USD",
    },
    {
        "symbol":      "CL",
        "description": "Crude Oil WTI",
        "yf_ticker":   "CL=F",
        "search_key":  "Crude Oil",
        "currency":    "USD",
    },
    {
        "symbol":      "ZN",
        "description": "10-Year T-Note",
        "yf_ticker":   "ZN=F",
        "search_key":  "US 10 Year T-Note",
        "currency":    "USD",
    },
    {
        "symbol":      "NQ",
        "description": "E-mini NASDAQ-100",
        "yf_ticker":   "NQ=F",
        "search_key":  "NASDAQ 100 E-Mini",
        "currency":    "USD",
    },
]


def discover_uics(get_fn) -> dict:
    """Search Saxo for each market and build the UIC map.

    get_fn: callable(path, params) -> dict  (thin wrapper around Saxo GET)
    Returns {symbol: {uic, description, currency, yf_ticker}}
    """
    result = {}
    for market in MARKETS:
        sym = market["symbol"]
        try:
            resp = get_fn("/ref/v1/instruments", {
                "Keywords":   market["search_key"],
                "AssetTypes": "CfdOnFutures",
                "$top":       10,
            })
            instruments = resp.get("Data", [])

            if not instruments:
                logger.warning(f"No CfdOnFutures found for {sym} ('{market['search_key']}')")
                continue

            # Prefer US exchange; otherwise take the first result
            best = None
            for inst in instruments:
                ex = inst.get("ExchangeId", "")
                if ex in ("XCME", "XCBT", "XNYM", "XNYS", "CME", "CBOT", "NYMEX"):
                    best = inst
                    break
            if best is None:
                best = instruments[0]

            uic = best.get("Identifier") or best.get("Uic")
            result[sym] = {
                "uic":         int(uic),
                "description": best.get("Description", market["description"]),
                "currency":    best.get("CurrencyCode", market["currency"]),
                "symbol":      sym,
                "yf_ticker":   market["yf_ticker"],
            }
            logger.info(f"  {sym}: UIC={uic}  {best.get('Description', '?')}")

        except Exception as exc:
            logger.warning(f"UIC discovery failed for {sym}: {exc}")

    return result


def load_universe(get_fn=None, refresh: bool = False) -> dict:
    """Return {symbol: {uic, description, currency, yf_ticker}}.

    Loads from cache if available; calls discover_uics(get_fn) if not.
    Pass refresh=True to force re-discovery.
    """
    if not refresh and os.path.exists(UIC_CACHE):
        try:
            with open(UIC_CACHE) as f:
                cached = json.load(f)
            if cached:
                logger.info(f"Futures universe: {len(cached)} markets from cache")
                return cached
        except Exception:
            pass

    if get_fn is None:
        raise RuntimeError(
            "No futures UIC cache found. Run `python futures/runner.py --discover` "
            "to populate data/futures_uic_cache.json via the Saxo API."
        )

    universe = discover_uics(get_fn)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(UIC_CACHE, "w") as f:
        json.dump(universe, f, indent=2)
    logger.info(f"Cached UICs for {len(universe)} futures markets → {UIC_CACHE}")
    return universe
