"""
futures/universe.py
-------------------
Instrument definitions for the futures trend-following strategy.

Saxo SIM does NOT have CfdOnFutures. Instead we use three instrument types:
  CfdOnIndex      — equity indices (continuous, no expiry, no roll)
  FxSpot          — precious metals (continuous, no expiry)
  ContractFutures — commodities + bonds (front-month, needs periodic refresh)

Run `python futures/runner.py --discover` to populate / refresh the cache.
Fixed-UIC instruments (ES, NQ, GC, SI) never need refreshing.
ContractFutures (CL, ZB, NG, ZN, ZC, ZW, ZS) refresh monthly on the 1st.
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
# fixed_uic=False → UIC must be discovered from Saxo instruments search
#
# contract_size: dollar change per 1-price-unit move per contract
#   CfdOnIndex / FxSpot: 1 (P&L = price_delta × qty directly in USD)
#   ZB / ZN:  1000  ($100k face, 1 point = $1,000)
#   CL:       1000  (1000 barrels, $1/bbl = $1,000)
#   NG:       10000 (10,000 MMBtu, $1 = $10,000)
#   ZC/ZW/ZS: 50    (5,000 bushels, 1 cent = $50; prices in cents/bushel)

MARKETS = [
    # ── US Equity Indices ────────────────────────────────────────────────
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
        "symbol":      "YM",
        "description": "Dow Jones Industrial Average CFD",
        "yf_ticker":   "DIA",
        "asset_type":  "CfdOnIndex",
        "search_key":  "Wall Street 30",    # Saxo name for US30.I
        "fixed_uic":   False,
        "currency":    "USD",
    },
    # ── European Equity Indices ──────────────────────────────────────────
    {
        "symbol":      "DAX",
        "description": "Germany 40 (DAX) Index CFD",
        "yf_ticker":   "EWG",
        "asset_type":  "CfdOnIndex",
        "search_key":  "Germany 40",
        "fixed_uic":   False,
        "currency":    "EUR",
    },
    {
        "symbol":      "CAC",
        "description": "France 40 (CAC 40) Index CFD",
        "yf_ticker":   "EWQ",
        "asset_type":  "CfdOnIndex",
        "search_key":  "France 40",
        "fixed_uic":   False,
        "currency":    "EUR",
    },
    {
        "symbol":        "EU50",
        "description":   "EURO STOXX 50 Index Futures (front-month)",
        "yf_ticker":     "IEV",
        "asset_type":    "ContractFutures",     # SIM only has this as ContractFutures, not CdfOnIndex
        "search_key":    "EURO STOXX 50 Index",
        "fixed_uic":     False,
        "currency":      "EUR",
        "contract_size": 10,    # €10 per index point
    },
    {
        "symbol":        "FTSE",
        "description":   "FTSE 100 Index Futures (front-month)",
        "yf_ticker":     "EWU",
        "asset_type":    "ContractFutures",     # SIM only has this as ContractFutures
        "search_key":    "FTSE 100 Index",
        "fixed_uic":     False,
        "currency":      "GBP",
        "contract_size": 10,    # £10 per index point
    },
    # ── Asian Equity Indices ─────────────────────────────────────────────
    {
        "symbol":      "HK50",
        "description": "Hong Kong 50 (Hang Seng) Index CFD",
        "yf_ticker":   "EWH",
        "asset_type":  "CfdOnIndex",
        "search_key":  "Hong Kong 50",
        "fixed_uic":   False,
        "currency":    "HKD",
    },
    {
        "symbol":        "NK225",
        "description":   "Nikkei 225 Index Futures (front-month)",
        "yf_ticker":     "EWJ",
        "asset_type":    "ContractFutures",     # SIM only has this as ContractFutures
        "search_key":    "Nikkei 225",
        "fixed_uic":     False,
        "currency":      "JPY",
        "contract_size": 100,   # ¥100 per index point (standard Nikkei contract)
    },
    # ── Precious Metals ──────────────────────────────────────────────────
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
        "symbol":      "SI",
        "description": "Silver Spot (XAG/USD)",
        "yf_ticker":   "SLV",
        "asset_type":  "FxSpot",
        "uic":         8178,        # XAGUSD — no expiry
        "fixed_uic":   True,
        "currency":    "USD",
        "skip_chart":  True,        # Saxo SIM chart API returns 400 for XAG/USD
    },
    # ── Energy ──────────────────────────────────────────────────────────
    {
        "symbol":        "CL",
        "description":   "WTI Crude Oil Futures (front-month)",
        "yf_ticker":     "USO",
        "asset_type":    "ContractFutures",
        "search_key":    "Light Sweet Crude Oil",
        "fixed_uic":     False,
        "currency":      "USD",
        "contract_size": 1000,
    },
    {
        "symbol":        "NG",
        "description":   "Natural Gas Futures (front-month)",
        "yf_ticker":     "UNG",
        "asset_type":    "ContractFutures",
        "search_key":    "Natural Gas",
        "fixed_uic":     False,
        "currency":      "USD",
        "contract_size": 10000,     # 10,000 MMBtu → $1 move = $10,000
    },
    # ── Agriculture ──────────────────────────────────────────────────────
    {
        "symbol":        "ZC",
        "description":   "Corn Futures (front-month)",
        "yf_ticker":     "CORN",
        "asset_type":    "ContractFutures",
        "search_key":    "Corn",
        "fixed_uic":     False,
        "currency":      "USD",
        "contract_size": 50,        # 5,000 bushels, price in cents → 1 cent = $50
    },
    {
        "symbol":        "ZW",
        "description":   "Wheat Futures (front-month)",
        "yf_ticker":     "WEAT",
        "asset_type":    "ContractFutures",
        "search_key":    "Wheat",
        "fixed_uic":     False,
        "currency":      "USD",
        "contract_size": 50,
    },
    {
        "symbol":        "ZS",
        "description":   "Soybean Futures (front-month)",
        "yf_ticker":     "SOYB",
        "asset_type":    "ContractFutures",
        "search_key":    "Soybeans",
        "fixed_uic":     False,
        "currency":      "USD",
        "contract_size": 50,
    },
]

# Long-only: equity indices and energy (structural long bias; short edge much weaker)
# Includes ContractFutures equity indices (EU50/FTSE/NK225 — SIM exposes these
# as ContractFutures, not CdfOnIndex; long-only bias unchanged).
LONG_ONLY_MARKETS = {
    "ES", "NQ", "YM", "DAX", "CAC", "EU50", "FTSE", "HK50", "NK225",
    "CL", "NG",
}

# Bidirectional: metals and grains (trend equally in both directions)
# Note: ZB / ZN removed — not available on Saxo SIM (Treasury futures not offered)
BIDIRECTIONAL_MARKETS = {"GC", "SI", "ZC", "ZW", "ZS"}


def discover_uics(get_fn) -> dict:
    """Build the UIC map, combining fixed UICs with Saxo instrument search.

    Handles three cases:
      fixed_uic=True + any asset_type   → UIC is hard-coded, no API call
      fixed_uic=False + CfdOnIndex      → search by Keywords + AssetTypes=CfdOnIndex
      fixed_uic=False + ContractFutures → search by Keywords + AssetTypes=ContractFutures,
                                          pick the nearest-expiry (front-month)

    get_fn: callable(path, params_dict) -> dict  (thin Saxo GET wrapper)
    Returns {symbol: {uic, asset_type, description, currency, yf_ticker, symbol, ...}}
    """
    result = {}
    for market in MARKETS:
        sym = market["symbol"]

        # ── Fixed UIC (continuous instrument, no API call needed) ─────────
        if market["fixed_uic"]:
            entry = {
                "uic":         market["uic"],
                "asset_type":  market["asset_type"],
                "description": market["description"],
                "currency":    market["currency"],
                "symbol":      sym,
                "yf_ticker":   market["yf_ticker"],
            }
            if market.get("skip_chart"):
                entry["skip_chart"] = True
            result[sym] = entry
            logger.info(f"  {sym}: UIC={market['uic']} ({market['asset_type']}) — fixed")
            continue

        asset_type = market["asset_type"]
        search_key = market.get("search_key", sym)

        try:
            if asset_type == "CfdOnIndex":
                # ── CfdOnIndex: search by keyword, take closest match ─────
                resp = get_fn("/ref/v1/instruments", {
                    "Keywords":   search_key,
                    "AssetTypes": "CfdOnIndex",
                    "$top":       5,
                })
                instruments = resp.get("Data", [])
                if not instruments:
                    logger.warning(f"  {sym}: no CfdOnIndex found for '{search_key}'")
                    continue
                inst = instruments[0]
                uic  = inst.get("Identifier") or inst.get("Uic")
                desc = inst.get("Description", market["description"])
                result[sym] = {
                    "uic":         int(uic),
                    "asset_type":  "CfdOnIndex",
                    "description": desc,
                    "currency":    inst.get("CurrencyCode", market["currency"]),
                    "symbol":      sym,
                    "yf_ticker":   market["yf_ticker"],
                }
                logger.info(f"  {sym}: UIC={uic} ({desc}) — CfdOnIndex discovered")

            elif asset_type == "ContractFutures":
                # ── ContractFutures: search and take nearest expiry ───────
                resp = get_fn("/ref/v1/instruments", {
                    "Keywords":   search_key,
                    "AssetTypes": "ContractFutures",
                    "$top":       5,
                })
                instruments = resp.get("Data", [])
                if not instruments:
                    logger.warning(f"  {sym}: no ContractFutures found for '{search_key}'")
                    continue

                def _expiry(i):
                    return i.get("ExpiryDate") or i.get("Expiry") or "9999"
                instruments.sort(key=_expiry)
                inst = instruments[0]
                uic  = inst.get("Identifier") or inst.get("Uic")
                desc = inst.get("Description", market["description"])
                result[sym] = {
                    "uic":           int(uic),
                    "asset_type":    "ContractFutures",
                    "description":   desc,
                    "currency":      inst.get("CurrencyCode", market["currency"]),
                    "symbol":        sym,
                    "yf_ticker":     market["yf_ticker"],
                    "contract_sym":  inst.get("Symbol", "?"),
                    "contract_size": market.get("contract_size", 1),
                }
                logger.info(f"  {sym}: UIC={uic} ({desc}) — front-month")

            else:
                logger.warning(f"  {sym}: unsupported asset_type '{asset_type}' with fixed_uic=False")

        except Exception as exc:
            logger.warning(f"  {sym}: discovery failed — {exc}")

    return result


def load_universe(get_fn=None, refresh: bool = False) -> dict:
    """Return {symbol: {uic, asset_type, description, currency, yf_ticker}}.

    On first run (no cache), get_fn is required to discover UICs.
    Subsequent runs load from cache. Pass refresh=True to force rediscovery
    (run monthly to keep ContractFutures on the front-month contract).
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
