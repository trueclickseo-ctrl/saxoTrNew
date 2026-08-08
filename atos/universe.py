"""
atos/universe.py
-----------------
US equity universe for ATOS — solid S&P 500 blue chips only.

Criteria for inclusion:
  1. S&P 500 constituent, market cap > $30B
  2. Average daily dollar volume > $200M (in/out without slippage)
  3. Established business — no near-term M&A/delist risk
  4. Core business understandable — no pure mining, no REITs, no biotech binary risk

108 stocks across 10 sectors. Reviewed 2026-08-08.
"""

SP500_TICKERS = [

    # ── Technology — mega-cap & software (10) ────────────────────────────────
    "AAPL",  # Apple — highest daily $ volume on earth
    "MSFT",  # Microsoft — cloud + AI, $3T, ultra-liquid
    "NVDA",  # Nvidia — AI chips, highest momentum stock in universe
    "AVGO",  # Broadcom — networking/custom AI chips, $800B+
    "ORCL",  # Oracle — cloud database, solid compounder
    "CSCO",  # Cisco — networking infrastructure, dividend, defensive
    "ADBE",  # Adobe — creative software, recurring revenue
    "CRM",   # Salesforce — enterprise CRM, high volume
    "NOW",   # ServiceNow — workflow automation, strong trend
    "INTU",  # Intuit — TurboTax/QuickBooks, pricing power

    # ── Technology — IT services, cybersecurity, semiconductors (8) ──────────
    "ACN",   # Accenture — IT consulting, $200B+, global
    "IBM",   # IBM — hybrid cloud/AI, defensive tech, dividend
    "PANW",  # Palo Alto Networks — cybersecurity, high momentum
    "SNPS",  # Synopsys — EDA software, essential chip design toolchain
    "TXN",   # Texas Instruments — analog chips, 50yr dividend growth
    "ADI",   # Analog Devices — analog/mixed-signal, pairs TXN
    "KLAC",  # KLA Corp — semiconductor inspection equipment
    "LRCX",  # Lam Research — semiconductor etch equipment

    # ── Communication Services (4) ───────────────────────────────────────────
    "GOOGL", # Alphabet — search + cloud, $2T, massive volume
    "META",  # Meta — social/AR, highest revenue growth in sector
    "NFLX",  # Netflix — streaming, $400B+, strong momentum
    "DIS",   # Disney — parks + streaming, defensive media

    # ── Consumer Discretionary (15) ──────────────────────────────────────────
    "AMZN",  # Amazon — e-commerce + AWS, $2T
    "TSLA",  # Tesla — EV leader, highest retail volume in universe
    "HD",    # Home Depot — home improvement, $400B, reliable trend
    "MCD",   # McDonald's — global QSR, inflation-pass-through
    "COST",  # Costco — membership retail, recession-resistant
    "NKE",   # Nike — global brand, sports apparel leader
    "BKNG",  # Booking Holdings — online travel, $150B+
    "TJX",   # TJX Companies — off-price retail, defensive discretionary
    "SBUX",  # Starbucks — global coffee, pricing power
    "LOW",   # Lowe's — home improvement, pairs HD for ranking
    "TGT",   # Target — general retail, $70B market cap
    "ORLY",  # O'Reilly Auto Parts — auto parts, recession-resistant
    "AZO",   # AutoZone — auto parts, buy-back machine
    "ROST",  # Ross Stores — off-price, defensive in downturns
    "CMG",   # Chipotle — fast casual, proven growth compounder

    # ── Consumer Staples (9) ─────────────────────────────────────────────────
    "WMT",   # Walmart — largest retailer, $700B, highest staples volume
    "PG",    # Procter & Gamble — household brands, ultra-low-vol
    "KO",    # Coca-Cola — beverages, Buffett holding, true defensive
    "PEP",   # PepsiCo — beverages + snacks, pairs KO
    "CL",    # Colgate-Palmolive — oral/personal care, global
    "MDLZ",  # Mondelez — snacks (Oreo, Cadbury), low tech correlation
    "GIS",   # General Mills — cereal/yogurt, stable FCF
    "KMB",   # Kimberly-Clark — tissue/personal care, ultra-low-vol
    "SYY",   # Sysco — food distribution, $30B, steady growth

    # ── Financials (15) ──────────────────────────────────────────────────────
    "JPM",   # JPMorgan — largest US bank, benchmark financial
    "V",     # Visa — payments network, 80%+ margins
    "MA",    # Mastercard — payments, pairs Visa
    "BAC",   # Bank of America — second-largest US bank
    "GS",    # Goldman Sachs — investment bank, high beta to markets
    "MS",    # Morgan Stanley — wealth + IB, $200B
    "BLK",   # BlackRock — world's largest asset manager
    "AXP",   # American Express — premium card, Buffett holding
    "SPGI",  # S&P Global — ratings + data, pricing power moat
    "CB",    # Chubb — global P&C insurer, $100B+
    "WFC",   # Wells Fargo — third-largest US bank, post-consent order
    "SCHW",  # Charles Schwab — largest US brokerage, high volume
    "MCO",   # Moody's — ratings duopoly, tracks SPGI
    "ICE",   # Intercontinental Exchange — exchange infrastructure
    "CME",   # CME Group — derivatives exchange, counter-cyclical

    # ── Healthcare (15) ──────────────────────────────────────────────────────
    "LLY",   # Eli Lilly — GLP-1 monopoly, highest HC momentum
    "UNH",   # UnitedHealth — managed care, largest HC company
    "JNJ",   # Johnson & Johnson — pharma + medtech, AAA rated
    "ABBV",  # AbbVie — Humira successor drugs, $300B+
    "MRK",   # Merck — Keytruda oncology, global pharma
    "TMO",   # Thermo Fisher — lab instruments, life science infrastructure
    "ISRG",  # Intuitive Surgical — robotic surgery monopoly
    "DHR",   # Danaher — life science tools, serial acquirer
    "MDT",   # Medtronic — cardiac/diabetes devices, defensive
    "AMGN",  # Amgen — mature biotech, $150B, consistent FCF + dividend
    "GILD",  # Gilead Sciences — HIV franchise, $100B, stable
    "CI",    # Cigna — managed care, $80B
    "ELV",   # Elevance Health — managed care (Anthem), $100B
    "BSX",   # Boston Scientific — cardiac/endo devices, $110B
    "SYK",   # Stryker — orthopedic/surgical, $140B, consistent compounder

    # ── Industrials (13) ─────────────────────────────────────────────────────
    "CAT",   # Caterpillar — construction/mining equipment, global cycle bellwether
    "HON",   # Honeywell — automation/aerospace, diversified industrial
    "RTX",   # RTX (Raytheon) — defense + aerospace engines
    "DE",    # Deere — agricultural equipment, pricing power
    "UPS",   # UPS — package delivery, global logistics
    "EMR",   # Emerson Electric — automation, 60yr dividend grower
    "ITW",   # Illinois Tool Works — diversified industrial, high ROIC
    "GE",    # GE Aerospace — jet engines, strong post-spin momentum
    "LMT",   # Lockheed Martin — F-35/missiles, defense, low-vol
    "NOC",   # Northrop Grumman — defense (B-21 bomber), steady
    "ETN",   # Eaton — power management/electrification, data center beneficiary
    "GD",    # General Dynamics — defense + Gulfstream jets
    "TDG",   # TransDigm — aerospace parts, strong pricing power

    # ── Energy (7) ───────────────────────────────────────────────────────────
    "XOM",   # ExxonMobil — largest US energy co, $500B+
    "CVX",   # Chevron — integrated major, post-Hess acquisition
    "COP",   # ConocoPhillips — pure E&P, best-in-class capital returns
    "EOG",   # EOG Resources — Permian E&P, top operator
    "MPC",   # Marathon Petroleum — largest US refiner, $60B
    "PSX",   # Phillips 66 — refining + midstream, $60B
    "VLO",   # Valero Energy — refining, highest beta to crack spreads

    # ── Semiconductors (5) ───────────────────────────────────────────────────
    "AMD",   # AMD — CPUs + AI GPUs, highest semi momentum after NVDA
    "QCOM",  # Qualcomm — mobile chips, diversifying to auto/IoT
    "MU",    # Micron — DRAM/NAND, AI memory demand
    "AMAT",  # Applied Materials — semiconductor equipment
    "ASML",  # ASML — EUV monopoly (Netherlands, ADR), essential

    # ── Materials (4) ────────────────────────────────────────────────────────
    "LIN",   # Linde — industrial gases, global duopoly, true low-vol
    "APD",   # Air Products — industrial gases, hydrogen economy
    "ECL",   # Ecolab — water/hygiene chemicals, recurring revenue
    "SHW",   # Sherwin-Williams — paint/coatings, pricing power

    # ── Utilities (3) ────────────────────────────────────────────────────────
    # Only the 3 largest — critical for the low-vol defense sleeve.
    "NEE",   # NextEra Energy — largest utility + wind/solar leader
    "SO",    # Southern Company — regulated utility, Southeast US
    "DUK",   # Duke Energy — regulated utility, 8M customers
]

# Deduplicated, sector order preserved
US_TICKERS = list(dict.fromkeys(SP500_TICKERS))

# ── Legacy / inactive — kept for backward-compatible imports ──────────────────
# OMX30/CPH25/DAX40 removed 2026-08-08 (both strategies failed backtests).
OMX30_TICKERS    = []
CPH25_TICKERS    = []
DAX40_TICKERS    = []
EUROPE_TICKERS   = []
COMMODITY_TICKERS = []
FOREX_TICKERS    = []

# ── Active universe ───────────────────────────────────────────────────────────
ATOS_UNIVERSE = US_TICKERS

MARKET_GROUPS = {
    "US Equities": set(US_TICKERS),
}

def market_of(ticker: str) -> str:
    for name, tickers in MARKET_GROUPS.items():
        if ticker in tickers:
            return name
    return "Unknown"

INITIAL_MARKET_WEIGHTS = {
    "US Equities": 1.0,
}

DETECTOR_MARKET_OVERRIDES: dict = {}
