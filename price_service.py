"""
price_service.py  —  Unified live price fetcher
-------------------------------------------------
Source:  Saxo SIM API  (/trade/v1/infoprices)
         Works 24/5 for FxSpot.  Returns None for instruments where Saxo SIM
         reports NoAccess (stocks, futures on SIM).

Usage:
    from price_service import fetch_prices, load_token

    prices = fetch_prices(instruments)
    # instruments = [{"symbol": "EURUSD", "uic": 21, "asset_type": "FxSpot"}, ...]
    # returns     = {"EURUSD": 1.1582, ...}
"""

import os, json, time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token.json")
SIM_BASE   = "https://gateway.saxobank.com/sim/openapi/"

# 2026-08-25: the real-money LIVE forex account needs its own token file and
# gateway -- every function below takes `env: str = "sim"`, unchanged default
# behavior for every existing caller (this module predates LIVE entirely).
LIVE_TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token_live.json")
LIVE_BASE       = "https://gateway.saxobank.com/openapi/"


# All 7 FX instruments (used by dashboards that need all rates even without open positions)
FX_INSTRUMENTS = [
    {"symbol": "EURUSD", "uic": 21,  "asset_type": "FxSpot"},
    {"symbol": "GBPUSD", "uic": 31,  "asset_type": "FxSpot"},
    {"symbol": "USDJPY", "uic": 42,  "asset_type": "FxSpot"},
    {"symbol": "AUDUSD", "uic": 4,   "asset_type": "FxSpot"},
    {"symbol": "USDCAD", "uic": 38,  "asset_type": "FxSpot"},
    {"symbol": "NZDUSD", "uic": 37,  "asset_type": "FxSpot"},
    {"symbol": "USDCHF", "uic": 39,  "asset_type": "FxSpot"},
    # Cross pairs Tier 1 — confirmed Saxo SIM 2026-08-17
    {"symbol": "EURGBP", "uic": 17,   "asset_type": "FxSpot"},
    {"symbol": "EURJPY", "uic": 18,   "asset_type": "FxSpot"},
    {"symbol": "GBPJPY", "uic": 26,   "asset_type": "FxSpot"},
    {"symbol": "AUDJPY", "uic": 2,    "asset_type": "FxSpot"},
    {"symbol": "CADJPY", "uic": 6,    "asset_type": "FxSpot"},
    # Cross pairs Tier 2 — confirmed Saxo SIM 2026-08-17
    {"symbol": "EURAUD", "uic": 12,   "asset_type": "FxSpot"},
    {"symbol": "EURNZD", "uic": 2072, "asset_type": "FxSpot"},
    {"symbol": "EURCAD", "uic": 13,   "asset_type": "FxSpot"},
    {"symbol": "EURCHF", "uic": 14,   "asset_type": "FxSpot"},
    {"symbol": "GBPAUD", "uic": 22,   "asset_type": "FxSpot"},
    {"symbol": "GBPCAD", "uic": 23,   "asset_type": "FxSpot"},
    {"symbol": "GBPCHF", "uic": 24,   "asset_type": "FxSpot"},
    {"symbol": "GBPNZD", "uic": 28,   "asset_type": "FxSpot"},
    {"symbol": "AUDCAD", "uic": 1,    "asset_type": "FxSpot"},
    {"symbol": "AUDCHF", "uic": 5027, "asset_type": "FxSpot"},
    {"symbol": "AUDNZD", "uic": 3,    "asset_type": "FxSpot"},
    {"symbol": "NZDJPY", "uic": 36,   "asset_type": "FxSpot"},
    {"symbol": "NZDCAD", "uic": 33,   "asset_type": "FxSpot"},
    {"symbol": "NZDCHF", "uic": 34,   "asset_type": "FxSpot"},
    {"symbol": "CHFJPY", "uic": 8,    "asset_type": "FxSpot"},
]


# ── Token ──────────────────────────────────────────────────────────

def load_token(env: str = "sim") -> str | None:
    """Return a valid access token or None if expired / missing."""
    token_file = TOKEN_FILE if env == "sim" else LIVE_TOKEN_FILE
    try:
        d = json.load(open(token_file))
        if time.time() > float(d.get("obtained_at", 0)) + int(d.get("expires_in", 1200)) - 60:
            return None
        return d.get("access_token")
    except Exception:
        return None


# ── Saxo single-instrument price ───────────────────────────────────

def _saxo_mid(token: str, uic: int, asset_type: str, env: str = "sim") -> float | None:
    """Fetch mid price from Saxo /trade/v1/infoprices. Returns None on failure.

    Retries once on 429 (rate-limit) with a short back-off. Uses a 15s
    timeout — the LIVE API can be slower than SIM under concurrent load.
    """
    import sys as _sys
    base = SIM_BASE if env == "sim" else LIVE_BASE
    for attempt in range(2):
        try:
            r = requests.get(
                base + "trade/v1/infoprices",
                headers={"Authorization": f"Bearer {token}"},
                params={"Uic": uic, "AssetType": asset_type, "FieldGroups": "Quote"},
                timeout=15,
            )
            if r.status_code == 429:
                wait = 3 * (attempt + 1)
                print(f"  [price_service] 429 UIC={uic} env={env} — retry in {wait}s",
                      file=_sys.stderr, flush=True)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return None
            q = r.json().get("Quote", {})
            mid = q.get("Mid")
            if mid is None and q.get("Ask") and q.get("Bid"):
                mid = (float(q["Ask"]) + float(q["Bid"])) / 2
            if mid is None:
                mid = q.get("LastTraded")
            return float(mid) if mid is not None else None
        except Exception:
            return None
    return None


# ── Main entry point ───────────────────────────────────────────────

def fetch_prices(instruments: list[dict], token: str = None, env: str = "sim") -> tuple[dict[str, float], str]:
    """
    Fetch live prices for a list of instruments from Saxo.

    Args:
        instruments: list of {"symbol": str, "uic": int, "asset_type": str}
        token:       Saxo access token (auto-loaded from the env's token file if None)
        env:         "sim" (default, unchanged) or "live" (real-money account)

    Returns:
        (prices, source) where source is "saxo" or "unavailable"
        prices = {"EURUSD": 1.1582, ...}
        Instruments where Saxo returns NoAccess (stocks, futures on SIM) are omitted.
    """
    if token is None:
        token = load_token(env=env)

    prices:  dict[str, float] = {}
    saxo_ok = False

    if token:
        # One sequential request per pair used to take ~10-30s+ for a full
        # 27-pair dashboard refresh (each round-trip is independent — no
        # ordering or rate-limit reason to serialize them). Fetch concurrently.
        jobs = [inst for inst in instruments if inst.get("uic")]

        # A single pass through a large batch (e.g. the forex dashboard's
        # ~90+ instruments: every open position + every EUR/USD conversion
        # pair, see forex_dashboard.py) drops a meaningful fraction to
        # per-request timeouts/transient hiccups under concurrent load —
        # confirmed live 2026-08-22 (35 of 94 failed in one run, a
        # different random subset each time, not the same instruments
        # repeatedly). One retry pass for just the misses recovers most of
        # them cheaply, and matters more now that this is the ONLY source
        # for forex's live currency-conversion rates (Yahoo fallback
        # removed from that path per explicit user direction — Saxo only
        # for live orders/dashboard, Yahoo stays historical/backtest-only).
        for attempt in range(2):
            misses = [inst for inst in jobs if inst["symbol"] not in prices]
            if not misses:
                break
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = {
                    pool.submit(_saxo_mid, token, inst["uic"], inst.get("asset_type", "FxSpot"), env): inst["symbol"]
                    for inst in misses
                }
                for fut in as_completed(futures):
                    sym = futures[fut]
                    px  = fut.result()
                    if px is not None:
                        prices[sym] = round(px, 5)
                        saxo_ok = True

    source = "saxo" if saxo_ok else "unavailable"
    return prices, source
