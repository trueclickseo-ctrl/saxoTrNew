"""
futures/universe.py
-------------------
Instrument definitions for the futures trend-following strategy.

Saxo SIM does NOT have CfdOnFutures. Instead we use three instrument types:
  CfdOnIndex    — equity indices (continuous, no expiry, no roll)
  FxSpot        — gold (XAUUSD — continuous, no expiry)
  ContractFutures — oil and bonds (front-month, needs periodic refresh)

Run `python futures/runner.py --discover` to populate / refresh the cache.
Fixed-UIC instruments (ES, NQ, GC) never need refreshing.
ContractFutures (CL, ZB) should be refreshed monthly to track the front-month.
"""

import json
import logging
import os

logger = logging.getLogger("futures.universe")

_ROOT     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR  = os.path.join(_ROOT, "data")
UIC_CACHE = os.path.join(DATA_DIR, "futures_uic_cache.json")

# ── Market definitions ────────────────────────────────────────────────────
# fixed_uic=True  → UIC is hard-coded (instrument is continuous, never expires)
# fixed_uic=False → UIC must be discovered from the front-month contract search

MARKETS = [
    {
        "symbol":      "ES",
        "description": "S&P 500 Index CFD",
        "yf_ticker":   "SPY",
        "asset_type":  "CfdOnIndex",
        "uic":         4913,        # US500.I — no expiry
        "fixed_uic":   True,
        "currency":    "USD",
    },
    {
        "symbol":      "NQ",
        "description": "NASDAQ-100 Index CFD",
        "yf_ticker":   "QQQ",
        "asset_type":  "CfdOnIndex",
        "uic":         4912,        # USNAS100.I — no expiry
        "fixed_uic":   True,
        "currency":    "USD",
    },
    {
        "symbol":      "GC",
        "description": "Gold Spot (XAU/USD)",
        "yf_ticker":   "GLD",
        "asset_type":  "FxSpot",
        "uic":         8176,        # XAUUSD — no expiry
        "fixed_uic":   True,
        "currency":    "USD",
    },
    {
        "symbol":      "CL",
        "description": "WTI Crude Oil Futures (front-month)",
        "yf_ticker":   "USO",
        "asset_type":  "ContractFutures",
        "search_key":  "Light Sweet Crude Oil",   # matches CLU6 / CLZ6
        "fixed_uic":   False,        # refresh monthly
        "currency":    "USD",
    },
    {
        "symbol":      "ZB",
        "description": "US 30-Year Treasury Bond Futures",
        "yf_ticker":   "TLT",
        "asset_type":  "ContractFutures",
        "search_key":  "Treasury",                    # matches ZBU6
        "fixed_uic":   False,        # refresh quarterly
        "currency":    "USD",
    },
]

# Which symbols trade long AND short (bonds + gold trend bidirectionally)
LONG_ONLY_MARKETS   = {"ES", "NQ", "CL"}
BIDIRECTIONAL_MARKETS = {"GC", "ZB"}


def discover_uics(get_fn) -> dict:
    """Build the UIC map, combining fixed UICs with front-month discovery.

    get_fn: callable(path, params_dict) -> dict  (thin Saxo GET wrapper)
    Returns {symbol: {uic, asset_type, description, currency, yf_ticker, symbol}}
    """
    result = {}
    for market in MARKETS:
        sym = market["symbol"]

        if market["fixed_uic"]:
            # Continuous instrument — UIC is hard-coded and permanent
            result[sym] = {
                "uic":         market["uic"],
                "asset_type":  market["asset_type"],
                "description": market["description"],
                "currency":    market["currency"],
                "symbol":      sym,
                "yf_ticker":   market["yf_ticker"],
            }
            logger.info(f"  {sym}: UIC={market['uic']} ({market['asset_type']}) — fixed")
            continue

        # ContractFutures — search for front-month (first result by expiry)
        try:
            # Fetch several contracts and take the nearest expiry (front-month).
            # $orderby is not supported by this endpoint — sort client-side.
            resp = get_fn("/ref/v1/instruments", {
                "Keywords":   market["search_key"],
                "AssetTypes": "ContractFutures",
                "$top":       5,
            })
            instruments = resp.get("Data", [])
            if not instruments:
                logger.warning(f"  {sym}: no ContractFutures found for '{market['search_key']}'")
                continue

            # Sort by expiry date (ExpiryDate field, ISO string) — front-month first
            def _expiry(i):
                return i.get("ExpiryDate") or i.get("Expiry") or "9999"
            instruments.sort(key=_expiry)
            inst = instruments[0]
            uic  = inst.get("Identifier") or inst.get("Uic")
            desc = inst.get("Description", market["description"])
            result[sym] = {
                "uic":         int(uic),
                "asset_type":  "ContractFutures",
                "description": desc,
                "currency":    inst.get("CurrencyCode", market["currency"]),
                "symbol":      sym,
                "yf_ticker":   market["yf_ticker"],
                "contract_sym": inst.get("Symbol", "?"),
            }
            logger.info(f"  {sym}: UIC={uic} ({desc}) — front-month")

        except Exception as exc:
            logger.warning(f"  {sym}: discovery failed — {exc}")

    return result


def load_universe(get_fn=None, refresh: bool = False) -> dict:
    """Return {symbol: {uic, asset_type, description, currency, yf_ticker}}.

    On first run (no cache), get_fn is required to discover front-month UICs.
    Subsequent runs load from cache. Pass refresh=True to force rediscovery
    (do this monthly to keep CL/ZB pointing at the front-month contract).
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
            "to populate data/futures_uic_cache.json."
        )

    universe = discover_uics(get_fn)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(UIC_CACHE, "w") as f:
        json.dump(universe, f, indent=2)
    logger.info(f"Saved UIC cache: {len(universe)} markets → {UIC_CACHE}")
    return universe
