"""
forex/universe.py
-----------------
Instrument definitions for the FX trend-following strategy.

All pairs use AssetType=FxSpot — confirmed available in Saxo SIM.
UICs are hard-coded from the Saxo SIM reference data (permanent for spot FX,
no expiry / roll required).

Run `python forex/runner.py --info` to verify UICs are live.
"""

# ── FX pair definitions ──────────────────────────────────────────────────────
# All confirmed FxSpot UICs from Saxo SIM /ref/v1/instruments endpoint.
# Bars arrive as CloseAsk/CloseBid (same format as XAUUSD in futures module).
# Amount in orders = units of base currency (e.g. EUR for EURUSD).

PAIRS = [
    {
        "symbol":      "EURUSD",
        "description": "Euro / US Dollar",
        "base":        "EUR",
        "quote":       "USD",
        "uic":         21,
        "yf_ticker":   "EURUSD=X",
        "pip_size":    0.0001,   # 1 pip = 0.0001
        "min_units":   1_000,    # Saxo minimum trade size
    },
    {
        "symbol":      "GBPUSD",
        "description": "British Pound / US Dollar",
        "base":        "GBP",
        "quote":       "USD",
        "uic":         31,
        "yf_ticker":   "GBPUSD=X",
        "pip_size":    0.0001,
        "min_units":   1_000,
    },
    {
        "symbol":      "USDJPY",
        "description": "US Dollar / Japanese Yen",
        "base":        "USD",
        "quote":       "JPY",
        "uic":         42,
        "yf_ticker":   "USDJPY=X",
        "pip_size":    0.01,    # JPY pairs: 1 pip = 0.01
        "min_units":   1_000,
    },
    {
        "symbol":      "AUDUSD",
        "description": "Australian Dollar / US Dollar",
        "base":        "AUD",
        "quote":       "USD",
        "uic":         4,
        "yf_ticker":   "AUDUSD=X",
        "pip_size":    0.0001,
        "min_units":   1_000,
    },
    {
        "symbol":      "USDCAD",
        "description": "US Dollar / Canadian Dollar",
        "base":        "USD",
        "quote":       "CAD",
        "uic":         38,
        "yf_ticker":   "USDCAD=X",
        "pip_size":    0.0001,
        "min_units":   1_000,
    },
    {
        "symbol":      "NZDUSD",
        "description": "New Zealand Dollar / US Dollar",
        "base":        "NZD",
        "quote":       "USD",
        "uic":         37,
        "yf_ticker":   "NZDUSD=X",
        "pip_size":    0.0001,
        "min_units":   1_000,
    },
    {
        "symbol":      "USDCHF",
        "description": "US Dollar / Swiss Franc",
        "base":        "USD",
        "quote":       "CHF",
        "uic":         39,
        "yf_ticker":   "USDCHF=X",
        "pip_size":    0.0001,
        "min_units":   1_000,
    },
]

ASSET_TYPE = "FxSpot"

# Lookup helpers
_BY_SYMBOL = {p["symbol"]: p for p in PAIRS}
_BY_UIC    = {p["uic"]:    p for p in PAIRS}


def get_pair(symbol: str) -> dict:
    return _BY_SYMBOL[symbol]


def get_all() -> list:
    return list(PAIRS)
