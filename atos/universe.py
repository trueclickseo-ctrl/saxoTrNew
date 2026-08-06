"""
atos/universe.py
-----------------
Defines the full multi-market universe for ATOS v1.
7 markets across 4 asset classes — selected for trend-following profitability,
low inter-market correlation, and full Saxo SIM API coverage.

DO NOT import config.py from here — ATOS universe is independent of the
legacy single-strategy bot's universe.
"""

# ── TIER 1: US Large-Cap Equities ─────────────────────────────────
# S&P 500 top names by liquidity + sector diversification.
# Best trend-following market in the world — highest historical Sharpe.
SP500_TICKERS = [
    # Mega-cap tech (highest liquidity, cleanest trends)
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO",
    # Financials (trend independently of tech)
    "JPM", "V", "MA", "BAC", "GS",
    # Healthcare (counter-cyclical, diversifier)
    "LLY", "UNH", "JNJ", "ABBV", "MRK",
    # Energy (correlated with oil — provides hedge)
    "XOM", "CVX", "COP",
    # Consumer + Industrial (different economic cycle)
    "HD", "MCD", "COST", "CAT", "HON",
    # Semiconductors (high-beta tech, different from FAANG)
    "AMD", "QCOM", "MU", "AMAT",
    # Other diversifiers
    "NFLX", "ADBE", "CRM", "DIS", "BA",
]

# NASDAQ 100 focused — high-beta tech, strong trending behavior
NASDAQ100_TICKERS = [
    "TSLA", "ASML", "CSCO", "INTC", "WMT",
    # Note: AAPL, MSFT, NVDA, AMZN, GOOGL, META, AVGO already in SP500_TICKERS
    # These are added as they're NASDAQ-specific or not in SP500 selection
]

# Combined US universe (deduplicated)
US_TICKERS = list(dict.fromkeys(SP500_TICKERS + NASDAQ100_TICKERS))

# ── TIER 2: European Equities ──────────────────────────────────────
# Top 15 most liquid OMX30 stocks (quality > quantity)
OMX30_TICKERS = [
    "ERIC-B.ST", "VOLV-B.ST", "INVE-B.ST", "SAND.ST",
    "SEB-A.ST",  "SWED-A.ST", "HM-B.ST",   "ABB.ST",
    "ALFA.ST",   "ASSA-B.ST", "AZN.ST",    "HEXA-B.ST",
    "NDA-SE.ST", "SKF-B.ST",  "SAAB-B.ST",
]

# Top 10 DAX40 by trading volume — European trend diversifier
DAX40_TICKERS = [
    "SAP.DE",  "SIE.DE", "ALV.DE", "AIR.DE", "IFX.DE",
    "MBG.DE",  "BMW.DE", "ADS.DE", "DTE.DE", "RHM.DE",
]

EUROPE_TICKERS = OMX30_TICKERS + DAX40_TICKERS

# OMX Copenhagen 25 (CPH25) — DKK. Only the constituents currently mapped to
# Saxo UICs in data/instrument_map.csv are listed here so they can trade
# immediately; extend this list after a reviewed lookup for the rest of OMXC25.
CPH25_TICKERS = [
    "NOVO-B.CO",   "DSV.CO",     "DANSKE.CO",  "MAERSK-B.CO", "MAERSK-A.CO",
    "ORSTED.CO",   "NSIS-B.CO",  "VWS.CO",     "CARL-B.CO",   "GMAB.CO",
]

# ── TIER 3: Commodities (via ETFs — available on Saxo as CfdOnEtc) ─
# Gold and Oil trend independently of equities — critical diversification.
# In 2020 COVID crash: stocks -30%, Gold +25%. In 2022: Oil +80%, stocks -20%.
COMMODITY_TICKERS = [
    "GLD",   # SPDR Gold Trust — primary safe-haven trend vehicle
    "SLV",   # iShares Silver Trust — follows gold, higher beta
    "USO",   # United States Oil Fund (WTI crude)
    "GDX",   # VanEck Gold Miners ETF — amplified gold exposure
]

# ── TIER 4: Forex pairs (via Yahoo Finance "=X" suffix) ───────────
# Trends for weeks/months based on central bank policy divergence.
# Zero commission on Saxo (spread only) — no minimum ticket fee.
FOREX_TICKERS = [
    "EURUSD=X",  # Most liquid forex pair, trends on ECB/Fed divergence
    "GBPUSD=X",  # Trending behavior since Brexit
    "USDJPY=X",  # Trends strongly on US-Japan interest rate differentials
]

# ── FULL ATOS UNIVERSE ─────────────────────────────────────────────
# Active markets (stock-only): US large-cap (S&P 500 + Nasdaq 100, combined),
# OMX30 (Stockholm, SEK), CPH25 (Copenhagen, DKK).
# DAX40 / Commodities / Forex are intentionally OUT of the active universe
# (Forex is handled by a separate quant system). Their ticker lists remain
# defined above for backward-compatible imports only.
ATOS_UNIVERSE = US_TICKERS + OMX30_TICKERS + CPH25_TICKERS

# Market group labels — used for per-market performance reporting
MARKET_GROUPS = {
    "US Equities":   set(US_TICKERS),
    "OMX30":         set(OMX30_TICKERS),
    "CPH25":         set(CPH25_TICKERS),
}

def market_of(ticker: str) -> str:
    for name, tickers in MARKET_GROUPS.items():
        if ticker in tickers:
            return name
    return "Unknown"

# ── Capital allocation weights (initial — adapt over time) ─────────
INITIAL_MARKET_WEIGHTS = {
    "US Equities":  0.50,   # Highest historical Sharpe
    "OMX30":        0.25,   # Home market (Stockholm)
    "CPH25":        0.25,   # Copenhagen diversifier
}

# Asset-class specific detector tuning
# Some detectors work better on certain asset classes
DETECTOR_MARKET_OVERRIDES = {
    "Forex": {
        "mean_reversion_enabled": False,  # Forex trends — doesn't mean-revert well
        "trend_weight_boost": 1.3,        # Trend detector gets extra weight
    },
    "Commodities": {
        "mean_reversion_enabled": False,  # Oil/Gold trend, don't fade them
        "breakout_weight_boost": 1.3,     # Commodities break out strongly
    },
}
