"""
atos/universe.py
-----------------
Defines the full multi-market universe for ATOS v1.
7 markets across 4 asset classes — selected for trend-following profitability,
low inter-market correlation, and full Saxo SIM API coverage.

DO NOT import config.py from here — ATOS universe is independent of the
legacy single-strategy bot's universe.
"""

# ── US Large-Cap Equities — 151 S&P 500 names across all 11 sectors ──────────
# Expanded from 61 → 151 on 2026-08-08. Rationale:
#   • Reversion needs signal frequency: more stocks = more independent dip events
#     (2.7 entries/month on 61 → target 4-6/month on 151)
#   • Blend needs ranking depth: deeper pool gives the momentum filter a real spread
#   • Stayed within S&P 500 (liquid, tight bid-ask, reliable Saxo fills)
#   • Added underrepresented sectors: REITs, Utilities, Materials, more Healthcare,
#     Energy, Industrials, Consumer Staples — genuine diversification, not just
#     adding correlated tech names
#   UICs for new tickers: run lookup_instruments.py before enabling live trading
SP500_TICKERS = [

    # ── Technology (10) ──────────────────────────────────────────────────────
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CSCO", "ADBE", "CRM", "NOW", "INTU",

    # ── Technology — IT services & enterprise (8 new) ────────────────────────
    "ACN",   # Accenture — IT consulting, steady compounder
    "IBM",   # IBM — hybrid cloud / AI, defensive tech
    "PANW",  # Palo Alto Networks — cybersecurity, high momentum name
    "SNPS",  # Synopsys — EDA software, essential semiconductor toolchain
    "TXN",   # Texas Instruments — analog chips, strong FCF, low-vol candidate
    "ADI",   # Analog Devices — analog/mixed-signal, pairs with TXN
    "KLAC",  # KLA Corp — semiconductor inspection equipment
    "LRCX",  # Lam Research — semiconductor etch/dep equipment

    # ── Communication Services (4) ───────────────────────────────────────────
    "GOOGL", "META", "NFLX", "DIS",

    # ── Consumer Discretionary (9 existing) ─────────────────────────────────
    "AMZN", "TSLA", "HD", "MCD", "COST", "NKE", "BKNG", "TJX", "SBUX",

    # ── Consumer Discretionary — more retail & housing (7 new) ──────────────
    "LOW",   # Lowe's — home improvement, pairs well with HD for momentum ranking
    "TGT",   # Target — general retail, independent from AMZN/COST dynamics
    "ORLY",  # O'Reilly Auto Parts — auto parts, recession-resistant momentum
    "AZO",   # AutoZone — auto parts, buy-back machine, low vol candidate
    "ROST",  # Ross Stores — off-price, strong momentum in consumer downturns
    "CMG",   # Chipotle — fast casual, high-momentum growth compounder
    "DHI",   # D.R. Horton — homebuilder, high beta to housing cycle

    # ── Consumer Staples — existing (4) ─────────────────────────────────────
    "WMT", "PG", "KO", "PEP",

    # ── Consumer Staples — expanded (10 new, key low-vol defense candidates) ─
    "CL",    # Colgate-Palmolive — global staples, true low-vol
    "MDLZ",  # Mondelez — snacks/bev, low correlation to tech drawdowns
    "GIS",   # General Mills — cereal/food, classic defensive
    "SYY",   # Sysco — food distribution, steady FCF
    "KMB",   # Kimberly-Clark — tissue/personal care, ultra-low-vol
    "CHD",   # Church & Dwight — household brands, consistent compounder
    "CLX",   # Clorox — cleaning products, spikes on volatility events
    "MKC",   # McCormick — spices, pricing power, low-vol
    "TSN",   # Tyson Foods — protein/food, counter-cyclical tendency
    "K",     # Kellanova (Kellogg) — cereal/snacks, defensive

    # ── Financials — existing (10) ───────────────────────────────────────────
    "JPM", "V", "MA", "BAC", "GS", "MS", "BLK", "AXP", "SPGI", "CB",

    # ── Financials — banks, brokers, exchanges (9 new) ───────────────────────
    "WFC",   # Wells Fargo — large bank, independent from JPM/BAC
    "COF",   # Capital One — credit card/consumer lending, high beta to cycle
    "USB",   # US Bancorp — regional, low-vol bank
    "PNC",   # PNC Financial — regional, low-vol bank
    "MCO",   # Moody's — ratings duopoly, strong pricing power, low-vol
    "ICE",   # Intercontinental Exchange — exchange infrastructure
    "CME",   # CME Group — derivatives exchange, counter-cyclical revenue
    "SCHW",  # Charles Schwab — brokerage, dips sharply then recovers (reversion)
    "BK",    # BNY Mellon — custody bank, true low-vol

    # ── Healthcare — existing (9) ────────────────────────────────────────────
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ISRG", "DHR", "MDT",

    # ── Healthcare — biotech, managed care, devices (12 new) ────────────────
    "VRTX",  # Vertex Pharma — cystic fibrosis monopoly, strong trend stock
    "REGN",  # Regeneron — biotech, high momentum periods
    "AMGN",  # Amgen — mature biotech, dividends, lower vol than VRTX/REGN
    "BMY",   # Bristol-Myers — diversified pharma, value-oriented
    "GILD",  # Gilead Sciences — antiviral/HIV, stable revenue, low-vol
    "CI",    # Cigna — managed care, pairs with UNH/ELV for ranking depth
    "ELV",   # Elevance Health — managed care (formerly Anthem)
    "HCA",   # HCA Healthcare — hospital operator, leveraged to utilization
    "BSX",   # Boston Scientific — medical devices, catheter/rhythm mgmt
    "SYK",   # Stryker — orthopedic devices, consistent compounder
    "IQV",   # IQVIA — healthcare data/CRO, strong secular trend
    "MCK",   # McKesson — pharma distribution, steady FCF, low-vol

    # ── Industrials — existing (7) ───────────────────────────────────────────
    "CAT", "HON", "RTX", "DE", "UPS", "EMR", "ITW",

    # ── Industrials — defense, automation, HVAC (10 new) ────────────────────
    "GE",    # GE Aerospace — pure-play jet engines, strong cycle exposure
    "LMT",   # Lockheed Martin — defense, low-vol, geopolitics-insensitive
    "NOC",   # Northrop Grumman — defense, high-margin, low-vol candidate
    "ETN",   # Eaton — power management/electrification, secular trend
    "PH",    # Parker-Hannifin — motion & control, global industrial bellwether
    "ROK",   # Rockwell Automation — factory automation, high-momentum periods
    "GD",    # General Dynamics — defense/Gulfstream, steady compounder
    "TDG",   # TransDigm — aerospace parts, pricing power, high momentum
    "CARR",  # Carrier Global — HVAC, building automation secular trend
    "IR",    # Ingersoll Rand — industrial equipment, compressed air/tools

    # ── Energy — existing (3) ────────────────────────────────────────────────
    "XOM", "CVX", "COP",

    # ── Energy — refining, E&P, services (8 new) ─────────────────────────────
    "EOG",   # EOG Resources — Permian E&P, best-in-class operator
    "PSX",   # Phillips 66 — refining/midstream, strong FCF
    "VLO",   # Valero Energy — refining, high-beta to crack spreads
    "MPC",   # Marathon Petroleum — refining, largest US refiner
    "OKE",   # ONEOK — natural gas midstream, dividend, low-vol
    "SLB",   # SLB (Schlumberger) — oilfield services, global cycle play
    "DVN",   # Devon Energy — Permian E&P, variable dividend model
    "HES",   # Hess Corp — E&P, Guyana exposure, acquisition target history

    # ── Semiconductors — existing (5) ────────────────────────────────────────
    "AMD", "QCOM", "MU", "AMAT", "ASML",

    # ── Materials (8 new — currently zero in universe) ───────────────────────
    "LIN",   # Linde — industrial gases, global duopoly, true low-vol compounder
    "APD",   # Air Products — industrial gases, hydrogen investment cycle
    "ECL",   # Ecolab — water/hygiene chemicals, steady pricing power
    "NEM",   # Newmont — gold mining, counter-cyclical, pairs with XOM/CVX
    "FCX",   # Freeport-McMoRan — copper, leveraged to EV/infrastructure demand
    "SHW",   # Sherwin-Williams — paint/coatings, pricing power, low-vol
    "PPG",   # PPG Industries — coatings, auto/industrial end-markets
    "VMC",   # Vulcan Materials — aggregates/construction, infrastructure beneficiary

    # ── Real Estate / REITs (10 new — currently zero in universe) ────────────
    # REITs are structurally counter-cyclical to tech — genuine diversification.
    # They also have distinct dip-and-recover patterns ideal for Reversion.
    "AMT",   # American Tower — cell tower REIT, secular mobile data growth
    "PLD",   # Prologis — industrial/logistics REIT, e-commerce beneficiary
    "EQIX",  # Equinix — data center REIT, strong momentum in AI buildout
    "CCI",   # Crown Castle — cell tower REIT, pairs with AMT for ranking
    "PSA",   # Public Storage — self-storage REIT, recession-resistant, low-vol
    "EXR",   # Extra Space Storage — self-storage, pairs with PSA
    "DLR",   # Digital Realty — data center REIT, AI infrastructure play
    "SPG",   # Simon Property — premium mall REIT, high-yield, dips sharply
    "O",     # Realty Income — net-lease REIT, monthly dividend, ultra-low-vol
    "EQR",   # Equity Residential — apartment REIT, interest-rate sensitive

    # ── Utilities (8 new — currently zero in universe) ───────────────────────
    # Utilities are the lowest-vol sector — critical for the defense sleeve.
    # When tech momentum fades, the defense sleeve should hold NEE/SO not NVDA.
    "NEE",   # NextEra Energy — largest utility, renewables leader
    "SO",    # Southern Company — regulated utility, Southeast US
    "DUK",   # Duke Energy — regulated utility, Carolinas/Midwest
    "D",     # Dominion Energy — regulated utility, Virginia
    "EXC",   # Exelon — nuclear/transmission utility, post-restructuring
    "AEP",   # American Electric Power — transmission-heavy utility
    "XEL",   # Xcel Energy — wind-heavy utility, clean energy transition
    "ED",    # Consolidated Edison — NYC utility, ultra-defensive
]

# Combined US universe (deduplicated, preserving sector order)
US_TICKERS = list(dict.fromkeys(SP500_TICKERS))

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
