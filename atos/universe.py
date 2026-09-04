"""
atos/universe.py
-----------------
US equity universe for ATOS — expanded 2026-08-14 from 108 → ~400 tickers,
then 2026-09-03 to ~424 (Nasdaq-100 + Dow-30 gap-fill). 2026-09-04 swing-fitness
audit: removed ~34 low-beta / thin-volume names (14 utilities, 11 ultra-defensive
staples, 2 thin insurance, 3 thin China ADRs, CLVT/APPN/APPF/PSKY), added 10
high-ADV momentum names → net ~400 tickers.
Spans the Dow Jones Industrial Average (30/30), the Nasdaq-100, and the large/
mid-cap core of the S&P 500 (the small-cap S&P tail is deliberately not carried).

Sectors: Technology, Financials, Energy, Healthcare, Consumer Discretionary,
         Industrials, Communication Services, Consumer Staples, Materials, Utilities.

Removed for swing-fitness (2026-09-04 audit — low beta / thin volume):
  Utilities (14): NEE SO DUK D AEP XEL EXC PEG ED ETR FE PPL AES CMS
  Staples (11):   PG KO CL KMB HRL CLX CPB MKC SJM GIS UL
  Insurance (2):  LNC CNA
  China ADRs (3): BILI TME HUYA
  Niche (4):      CLVT APPN APPF PSKY

Excluded (confirmed delistings/acquisitions as of Aug 2026):
  ATVI  — acquired by Microsoft (Oct 2023)
  VMW   — acquired by Broadcom (Nov 2023)
  SPLK  — acquired by Cisco (Mar 2024)
  K     — Kellanova acquired by Mars (Aug 2024)
  PXD   — acquired by ExxonMobil (Oct 2024)
  HES   — acquired by Chevron (Oct 2024)
  MRO   — acquired by ConocoPhillips (Nov 2024)
  DFS   — acquired by Capital One (May 2025)
  TWTR  — went private (Oct 2022)
  COG   — renamed CTRA (Coterra Energy); not on Saxo SIM

Run lookup_missing.py after any universe change to fill new UICs.
"""

SP500_TICKERS = [

    # ── Technology — mega-cap & cloud (10) ───────────────────────────────────
    "AAPL",  # Apple — highest daily $ volume on earth
    "MSFT",  # Microsoft — cloud + AI, $3T
    "NVDA",  # Nvidia — AI chips, highest momentum
    "AVGO",  # Broadcom — networking/custom AI chips
    "ORCL",  # Oracle — cloud database, compounder
    "CSCO",  # Cisco — networking, dividend, defensive
    "ADBE",  # Adobe — creative software, recurring revenue
    "CRM",   # Salesforce — enterprise CRM
    "NOW",   # ServiceNow — workflow automation

    # ── Technology — enterprise software & IT services (8) ───────────────────
    "ACN",   # Accenture — IT consulting, $200B+
    "IBM",   # IBM — hybrid cloud/AI, defensive, dividend
    "PANW",  # Palo Alto Networks — cybersecurity platform
    "FTNT",  # Fortinet — network security appliances
    "ADSK",  # Autodesk — CAD/design software, SaaS transition
    "CDNS",  # Cadence Design Systems — EDA software
    "SNPS",  # Synopsys — EDA, semiconductor toolchain

    # ── Technology — semiconductors (14) ─────────────────────────────────────
    "AMD",   # AMD — CPUs + AI GPUs
    "QCOM",  # Qualcomm — mobile chips, auto/IoT
    "MU",    # Micron — DRAM/NAND, AI memory
    "AMAT",  # Applied Materials — semiconductor equipment
    "ASML",  # ASML — EUV monopoly (Dutch ADR)
    "LRCX",  # Lam Research — etch equipment
    "KLAC",  # KLA Corp — semiconductor inspection
    "TXN",   # Texas Instruments — analog chips, 50yr dividend growth
    "ADI",   # Analog Devices — analog/mixed-signal
    "MRVL",  # Marvell Technology — data infra chips
    "MCHP",  # Microchip Technology — MCUs, embedded
    "ON",    # ON Semiconductor — power/auto chips
    "NXPI",  # NXP Semiconductors — auto/IoT chips (Dutch ADR)
    "TER",   # Teradyne — semiconductor test equipment
    "QRVO",  # Qorvo — RF chips for mobile
    "SWKS",  # Skyworks Solutions — RF/wireless chips

    # ── Technology — hardware & storage (6) ──────────────────────────────────
    "HPQ",   # HP Inc. — PCs/printing, high buybacks
    "DELL",  # Dell Technologies — infrastructure + AI servers
    "HPE",   # HP Enterprise — servers/networking
    "NTAP",  # NetApp — cloud data storage
    "STX",   # Seagate — HDD storage, dividend
    "WDC",   # Western Digital — HDD/NAND, pairs STX

    # ── Technology — networking & cloud infrastructure ────────────────────────
    "ANET",  # Arista Networks — cloud networking
    "GDDY",  # GoDaddy — web hosting/domains, SMB SaaS
    "DOCN",  # DigitalOcean — cloud for SMBs (marginal, monitor fills)

    # ── Technology — cybersecurity & cloud software (10) ─────────────────────
    "CRWD",  # CrowdStrike — endpoint security
    "ZS",    # Zscaler — zero-trust network security
    "NET",   # Cloudflare — network security/CDN
    "OKTA",  # Okta — identity management
    "SNOW",  # Snowflake — cloud data platform
    "DDOG",  # Datadog — observability/APM
    "MDB",   # MongoDB — document database
    "WDAY",  # Workday — HR/finance cloud
    "TEAM",  # Atlassian — dev collaboration (Jira)

    # ── Technology — SaaS & developer tools (8) ──────────────────────────────
    "SHOP",  # Shopify — e-commerce platform (Canadian, NYSE listed)
    "PLTR",  # Palantir — data analytics/AI for gov & enterprise
    "HUBS",  # HubSpot — marketing/CRM software
    "MNDY",  # Monday.com — work OS
    "TWLO",  # Twilio — communications APIs
    "DOCU",  # DocuSign — e-signature, SaaS
    "VEEV",  # Veeva Systems — life sciences cloud
    "ZM",    # Zoom — video conferencing, mean-reversion candidate

    # ── Technology — fintech & payments (5) ──────────────────────────────────
    "PYPL",  # PayPal — digital payments
    "XYZ",   # Block (formerly SQ, rebranded Jan 2025) — Cash App + payments
    "EBAY",  # eBay — e-commerce marketplace
    "MTCH",  # Match Group — online dating (Tinder/Hinge)
    "KDP",   # Keurig Dr Pepper — beverages + direct-to-consumer

    # ── Technology — mobility & consumer internet (8) ────────────────────────
    "UBER",  # Uber — ride-share + delivery platform
    "LYFT",  # Lyft — US ride-share
    "DASH",  # DoorDash — food delivery
    "ABNB",  # Airbnb — short-term rentals
    "EXPE",  # Expedia — online travel
    "ETSY",  # Etsy — handmade marketplace
    "W",     # Wayfair — online furniture, high-beta
    "TTD",   # The Trade Desk — programmatic advertising

    # ── Technology — gaming & media software (3) ─────────────────────────────
    "RBLX",  # Roblox — user-generated gaming platform
    # "EA",  # Electronic Arts — not available in Saxo SIM (no instrument found)
    "TTWO",  # Take-Two Interactive — GTA publisher

    # ── Technology — international tech ADRs (7) ─────────────────────────────
    "BIDU",  # Baidu — Chinese search/AI (NASDAQ ADR)
    "BABA",  # Alibaba — Chinese e-commerce (NYSE ADR)
    "NTES",  # NetEase — Chinese gaming/cloud (NASDAQ ADR)
    "JD",    # JD.com — Chinese e-commerce (NASDAQ ADR)
    "PDD",   # PDD Holdings (Temu/Pinduoduo) — NASDAQ ADR
    "MELI",  # MercadoLibre — LatAm e-commerce (NASDAQ)
    "SE",    # Sea Limited — SE Asia gaming/e-com (NYSE)

    # ── Technology — ad-tech ─────────────────────────────────────────────────
    "PUBM",  # PubMatic — sell-side ad platform

    # ── Technology — clean energy tech (4) ───────────────────────────────────
    "FSLR",  # First Solar — US solar panels, IRA beneficiary
    "ENPH",  # Enphase Energy — solar microinverters
    "RUN",   # Sunrun — residential solar installer
    "SEDG",  # SolarEdge — solar inverters

    # ── Technology — EV (2) ──────────────────────────────────────────────────
    "RIVN",  # Rivian — EV trucks/delivery vans
    "LCID",  # Lucid Group — luxury EV

    # ── Communication Services — existing (4) ────────────────────────────────
    "GOOGL", # Alphabet — search + cloud, $2T
    "META",  # Meta — social/AR
    "NFLX",  # Netflix — streaming
    "DIS",   # Disney — parks + streaming

    # ── Communication Services — telecom & cable (7) ─────────────────────────
    "CMCSA", # Comcast — cable/broadband/NBC
    "VZ",    # Verizon — telecom, high yield
    "T",     # AT&T — telecom, post-Warner spinoff
    "TMUS",  # T-Mobile — fastest-growing US carrier
    "CHTR",  # Charter Communications — cable
    "FOXA",  # Fox Corporation — news/sports media

    # ── Communication Services — streaming & social ───────────────────────────
    "SNAP",  # Snap — social/camera, high-beta
    "PINS",  # Pinterest — visual discovery
    "SPOT",  # Spotify — audio streaming
    "ROKU",  # Roku — streaming OS/ad platform
    "WBD",   # Warner Bros. Discovery — streaming + HBO
    "U",     # Unity Software — 3D engine for gaming/AR

    # ── Consumer Discretionary — existing (15) ───────────────────────────────
    "AMZN",  # Amazon — e-commerce + AWS
    "TSLA",  # Tesla — EV leader
    "HD",    # Home Depot — home improvement
    "MCD",   # McDonald's — global QSR
    "COST",  # Costco — membership retail
    "NKE",   # Nike — global brand
    "BKNG",  # Booking Holdings — online travel
    "TJX",   # TJX Companies — off-price retail
    "SBUX",  # Starbucks — global coffee
    "LOW",   # Lowe's — home improvement
    "TGT",   # Target — general retail
    "ORLY",  # O'Reilly Auto Parts — recession-resistant
    "AZO",   # AutoZone — auto parts, buyback machine
    "ROST",  # Ross Stores — off-price, defensive
    "CMG",   # Chipotle — fast casual

    # ── Consumer Discretionary — restaurants (2 new) ─────────────────────────
    "YUM",   # Yum! Brands — KFC/Pizza Hut/Taco Bell
    "DPZ",   # Domino's Pizza — delivery brand

    # ── Consumer Discretionary — value retail (3 new) ────────────────────────
    "DG",    # Dollar General — rural discount retail
    "DLTR",  # Dollar Tree — dollar-store retail
    "BBY",   # Best Buy — consumer electronics

    # ── Consumer Discretionary — specialty retail (5 new) ────────────────────
    "TSCO",  # Tractor Supply — farm/ranch retail
    "ULTA",  # Ulta Beauty — beauty specialty
    "LULU",  # Lululemon — premium athletic wear
    "KMX",   # CarMax — used auto retail
    "AN",    # AutoNation — auto dealerships

    # ── Consumer Discretionary — apparel & accessories (9 new) ───────────────
    "PVH",   # PVH Corp — Calvin Klein/Tommy Hilfiger
    "RL",    # Ralph Lauren — luxury American fashion
    "VFC",   # VF Corporation — The North Face/Timberland
    # "SKX",  # Skechers — not found on Yahoo Finance (delisted/OTC?)
    "CROX",  # Crocs — foam footwear, viral brand
    # "HBI",  # Hanesbrands — no Yahoo data (bankruptcy/delisted 2024)
    "LEVI",  # Levi Strauss — denim/apparel (NYSE)
    "HOG",   # Harley-Davidson — motorcycles, aspirational brand
    "GRMN",  # Garmin — GPS/wearables, aviation exposure

    # ── Consumer Discretionary — hospitality & gaming (4 new) ────────────────
    "HLT",   # Hilton Hotels — asset-light franchise
    "MAR",   # Marriott — largest hotel franchise
    "MGM",   # MGM Resorts — gaming/hospitality + BetMGM
    "LVS",   # Las Vegas Sands — Macau/Singapore gaming

    # ── Consumer Discretionary — cruise lines (2 new) ────────────────────────
    "CCL",   # Carnival — cruise line, high-beta
    "RCL",   # Royal Caribbean — cruise line

    # ── Consumer Discretionary — autos (2 new) ───────────────────────────────
    "F",     # Ford Motor — legacy auto + EV transition
    "GM",    # General Motors — large buybacks, cyclical

    # ── Consumer Discretionary — entertainment & toys (3 new) ────────────────
    "SONY",  # Sony Group — PlayStation/music (NYSE ADR)
    "HAS",   # Hasbro — toys/entertainment (Transformers/GI Joe)
    "MAT",   # Mattel — Barbie/Hot Wheels, turnaround

    # ── Consumer Staples — existing (kept: liquid/vol adequate) ─────────────────
    "WMT",   # Walmart — largest retailer, high volume + momentum
    "PEP",   # PepsiCo — beverages + snacks, higher beta than KO
    "MDLZ",  # Mondelez — snacks (Oreo/Cadbury)
    "SYY",   # Sysco — food distribution

    # ── Consumer Staples — packaged food & beverage (kept: volume/vol adequate) ─
    "KHC",   # Kraft Heinz — packaged foods, high yield
    "CHD",   # Church & Dwight — household brands
    "ADM",   # Archer Daniels Midland — agri-processing, commodity catalysts
    "KR",    # Kroger — grocery retail, M&A catalyst history
    "ACI",   # Albertsons — grocery retail
    "CAG",   # Conagra Brands — packaged foods
    "HSY",   # Hershey — confectionery, pricing power

    # ── Consumer Staples — beverages & tobacco (kept) ────────────────────────
    "TAP",   # Molson Coors — beer, some volume + catalyst (earnings)
    "STZ",   # Constellation Brands — beer (Modelo/Corona in US)
    "MO",    # Altria — cigarettes/tobacco, high yield + RSI-reversal candidate
    "PM",    # Philip Morris International — cigarettes/IQOS
    "TSN",   # Tyson Foods — protein/chicken, commodity-driven momentum
    "COKE",  # Coca-Cola Consolidated — regional bottler
    "BF-B",  # Brown-Forman B — Jack Daniel's/spirits

    # ── Financials — existing (15) ───────────────────────────────────────────
    "JPM",   # JPMorgan — largest US bank
    "V",     # Visa — payments network, 80%+ margins
    "MA",    # Mastercard — payments, pairs Visa
    "BAC",   # Bank of America — second-largest US bank
    "GS",    # Goldman Sachs — investment bank
    "MS",    # Morgan Stanley — wealth + IB
    "BLK",   # BlackRock — world's largest asset manager
    "AXP",   # American Express — premium card, Buffett
    "SPGI",  # S&P Global — ratings + data
    "CB",    # Chubb — global P&C insurer
    "WFC",   # Wells Fargo — third-largest US bank
    "SCHW",  # Charles Schwab — largest US brokerage
    "MCO",   # Moody's — ratings duopoly
    "ICE",   # Intercontinental Exchange — exchange infra
    "CME",   # CME Group — derivatives exchange

    # ── Financials — large/regional banks (9 new) ────────────────────────────
    "C",     # Citigroup — global bank, restructuring
    "COF",   # Capital One — credit cards/consumer lending
    "USB",   # US Bancorp — super-regional bank
    "TFC",   # Truist Financial — Southeast regional
    "PNC",   # PNC Financial — regional bank
    "FITB",  # Fifth Third Bancorp — Midwest regional
    "RF",    # Regions Financial — Southeast regional
    "KEY",   # KeyCorp — regional, interest-rate sensitive
    "HBAN",  # Huntington Bancshares — Midwest regional

    # ── Financials — diversified banks & trust (4 new) ───────────────────────
    "STT",   # State Street — custody bank/ETF business
    "MTB",   # M&T Bank — regional, Mid-Atlantic
    "CFG",   # Citizens Financial — regional bank
    "ZION",  # Zions Bancorporation — Western US regional

    # ── Financials — insurance (kept: volume/vol adequate) ───────────────────
    "MET",   # MetLife — life/annuities
    "PRU",   # Prudential Financial — life insurance/asset mgmt
    "AIG",   # AIG — P&C insurance, restructured
    "TRV",   # Travelers — P&C insurance, Dow component
    "ALL",   # Allstate — auto/home insurance
    "PGR",   # Progressive — auto insurance, fastest-growing
    "AFL",   # Aflac — supplemental insurance, Japan exposure
    "UNM",   # Unum Group — disability/life insurance (marginal, monitor fills)
    "HIG",   # Hartford Financial — P&C + group benefits

    # ── Financials — financial services & fintech (9 new) ────────────────────
    "SYF",   # Synchrony Financial — private label credit cards
    "HOOD",  # Robinhood Markets — retail brokerage/crypto
    "SOFI",  # SoFi Technologies — neobank
    "BR",    # Broadridge Financial — investor communications
    "CBOE",  # Cboe Global Markets — options exchange
    "FIS",   # Fidelity National Information Services — payments tech
    "FISV",  # Fiserv — payment processing, POS systems
    "GPN",   # Global Payments — merchant processing
    "WU",    # Western Union — money transfer
    "NDAQ",  # Nasdaq Inc. — exchange + data/analytics
    "MKTX",  # MarketAxess — electronic bond trading
    "NTRS",  # Northern Trust — wealth mgmt/custody

    # ── Financials — REITs (4 new) ────────────────────────────────────────────
    "AMT",   # American Tower — cell tower REIT
    "EQIX",  # Equinix — data center REIT, AI buildout
    "DLR",   # Digital Realty — data center REIT
    "CCI",   # Crown Castle — cell tower REIT

    # ── Healthcare — existing (15) ───────────────────────────────────────────
    "LLY",   # Eli Lilly — GLP-1 monopoly
    "UNH",   # UnitedHealth — managed care, largest HC
    "JNJ",   # Johnson & Johnson — pharma + medtech
    "ABBV",  # AbbVie — Humira successor drugs
    "MRK",   # Merck — Keytruda oncology
    "TMO",   # Thermo Fisher — lab instruments
    "ISRG",  # Intuitive Surgical — robotic surgery
    "DHR",   # Danaher — life science tools
    "MDT",   # Medtronic — cardiac/diabetes devices
    "AMGN",  # Amgen — mature biotech, dividend
    "GILD",  # Gilead Sciences — HIV franchise
    "CI",    # Cigna — managed care
    "ELV",   # Elevance Health — managed care (Anthem)
    "BSX",   # Boston Scientific — cardiac/endo devices
    "SYK",   # Stryker — orthopedic/surgical

    # ── Healthcare — large pharma & biotech (6 new) ──────────────────────────
    "PFE",   # Pfizer — large-cap pharma, pipeline
    "BMY",   # Bristol-Myers Squibb — diversified pharma
    "MRNA",  # Moderna — mRNA platform, high-beta
    "REGN",  # Regeneron — biotech, high momentum
    "VRTX",  # Vertex Pharma — cystic fibrosis monopoly
    "BIIB",  # Biogen — neurology/Alzheimer's

    # ── Healthcare — managed care & distribution (6 new) ─────────────────────
    "HUM",   # Humana — Medicare Advantage
    "CVS",   # CVS Health — pharmacy/PBM/insurance
    # "WBA",  # Walgreens — no Yahoo data (went private Aug 2024)
    "MCK",   # McKesson — pharma distribution
    "CAH",   # Cardinal Health — pharma distribution
    "IQV",   # IQVIA — healthcare data/CRO

    # ── Healthcare — medical devices (10 new) ────────────────────────────────
    "EW",    # Edwards Lifesciences — heart valves
    "ZBH",   # Zimmer Biomet — orthopedic implants
    "BAX",   # Baxter International — renal/hospital products
    "BDX",   # Becton Dickinson — medical devices/diagnostics
    "ALGN",  # Align Technology — Invisalign, dental
    "DXCM",  # DexCom — continuous glucose monitoring
    "GEHC",  # GE HealthCare — imaging/diagnostics
    "PEN",   # Penumbra — neurovascular/blood clot devices
    # "MASI",  # Masimo — no Yahoo data (ticker changed after acquisition)

    # ── Healthcare — diagnostics & tools (6 new) ─────────────────────────────
    "HCA",   # HCA Healthcare — hospital operator
    "THC",   # Tenet Healthcare — hospital operator
    "LH",    # LabCorp — lab diagnostics
    "DGX",   # Quest Diagnostics — lab diagnostics
    "A",     # Agilent Technologies — lab instruments
    "IDXX",  # IDEXX Labs — veterinary diagnostics
    "ZTS",   # Zoetis — animal health
    "ILMN",  # Illumina — genomic sequencing

    # ── Healthcare — genomics & early-stage biotech (5 new) ──────────────────
    "CRSP",  # CRISPR Therapeutics — gene editing
    "EDIT",  # Editas Medicine — gene editing
    "NTLA",  # Intellia Therapeutics — in vivo gene editing
    # "EXAS",  # Exact Sciences — no Yahoo data (ticker issue, skip)
    "RGEN",  # Repligen — bioprocessing tools
    "NVCR",  # NovaCure — tumor treating fields (oncology)

    # ── Industrials — existing (13) ──────────────────────────────────────────
    "CAT",   # Caterpillar — construction/mining equipment
    "HON",   # Honeywell — automation/aerospace
    "RTX",   # RTX (Raytheon) — defense + aerospace
    "DE",    # Deere — agricultural equipment
    "UPS",   # UPS — package delivery
    "EMR",   # Emerson Electric — automation, 60yr dividend
    "ITW",   # Illinois Tool Works — diversified industrial
    "GE",    # GE Aerospace — jet engines
    "LMT",   # Lockheed Martin — F-35/missiles
    "NOC",   # Northrop Grumman — defense (B-21)
    "ETN",   # Eaton — power management/data center
    "GD",    # General Dynamics — defense + Gulfstream
    "TDG",   # TransDigm — aerospace parts, pricing power

    # ── Industrials — transport & diversified (8 new) ────────────────────────
    "BA",    # Boeing — aerospace/defense, high-beta
    "MMM",   # 3M — diversified industrial, restructuring
    "UNP",   # Union Pacific — Class I railroad
    "FDX",   # FedEx — express/freight, economic indicator
    "PH",    # Parker-Hannifin — motion & control
    "ROK",   # Rockwell Automation — factory automation
    "CSX",   # CSX Corporation — railroad, East US
    "NSC",   # Norfolk Southern — railroad, pairs CSX

    # ── Industrials — airlines (5 new) ────────────────────────────────────────
    "DAL",   # Delta Air Lines — premium airline
    "UAL",   # United Airlines — global carrier
    "LUV",   # Southwest Airlines — low-cost carrier
    "ALK",   # Alaska Air Group — West Coast carrier
    "JBLU",  # JetBlue Airways — ultra-low-cost

    # ── Industrials — waste & engineering (7 new) ─────────────────────────────
    "WM",    # Waste Management — defensive infrastructure
    "RSG",   # Republic Services — waste management
    "J",     # Jacobs Solutions — engineering & construction
    "FLR",   # Fluor — global engineering, high-beta
    "ACM",   # AECOM — infrastructure engineering
    "DOV",   # Dover — diversified industrial manufacturing
    "CMI",   # Cummins — engines/power systems

    # ── Industrials — HVAC & equipment (4 new) ────────────────────────────────
    "IR",    # Ingersoll Rand — compressed air/industrial tools
    "CARR",  # Carrier Global — HVAC/refrigeration
    "OTIS",  # Otis Worldwide — elevators/escalators
    "TEX",   # Terex — aerial work platforms, cranes

    # ── Industrials — building products & materials (4 new) ───────────────────
    "MAS",   # Masco — cabinets/plumbing/coatings
    "MLM",   # Martin Marietta Materials — aggregates
    "VMC",   # Vulcan Materials — aggregates, infrastructure
    "EXP",   # Eagle Materials — cement/wallboard

    # ── Industrials — homebuilders (6 new) ────────────────────────────────────
    "DHI",   # D.R. Horton — largest US homebuilder
    "LEN",   # Lennar — second-largest homebuilder
    "PHM",   # PulteGroup — homebuilder, entry-to-luxury
    "NVR",   # NVR Inc. — premium homebuilder, no land risk
    "KBH",   # KB Home — entry-level homebuilder
    "TOL",   # Toll Brothers — luxury homebuilder

    # ── Energy — existing (7) ────────────────────────────────────────────────
    "XOM",   # ExxonMobil — largest US energy co
    "CVX",   # Chevron — integrated major
    "COP",   # ConocoPhillips — pure E&P
    "EOG",   # EOG Resources — Permian E&P
    "MPC",   # Marathon Petroleum — largest US refiner
    "PSX",   # Phillips 66 — refining + midstream
    "VLO",   # Valero Energy — refining

    # ── Energy — E&P & oilfield services (9 new) ─────────────────────────────
    "SLB",   # SLB (Schlumberger) — oilfield services
    "HAL",   # Halliburton — oilfield services
    "OXY",   # Occidental Petroleum — Permian + chemicals
    "DVN",   # Devon Energy — Permian E&P
    "BKR",   # Baker Hughes — oilfield tech/LNG
    "APA",   # APA Corp — international E&P
    "RRC",   # Range Resources — Appalachian nat gas
    "EQT",   # EQT Corp — largest US nat gas producer
    "LNG",   # Cheniere Energy — LNG export

    # ── Energy — midstream & pipelines (3 new) ───────────────────────────────
    "KMI",   # Kinder Morgan — nat gas pipelines
    "WMB",   # Williams Companies — nat gas midstream
    "OKE",   # ONEOK — nat gas midstream, dividend

    # ── Energy — power generation (1 new) ─────────────────────────────────────
    "NRG",   # NRG Energy — competitive power generation

    # ── Energy — Canadian energy (4 new) ──────────────────────────────────────
    "ENB",   # Enbridge — Canadian pipelines (NYSE listed)
    "TRP",   # TC Energy — Canadian pipelines (NYSE listed)
    "CNQ",   # Canadian Natural Resources (NYSE listed)
    "SU",    # Suncor Energy — Canadian oil sands (NYSE listed)

    # ── Utilities — marginal (keep; high headline risk + vol vs pure defensives) ──
    "PCG",   # PG&E — California utility (post-bankruptcy, fire-liability headlines)
    "SRE",   # Sempra — California/Texas utility + LNG export, higher ATR than peers

    # ── Materials — existing (4) ─────────────────────────────────────────────
    "LIN",   # Linde — industrial gases, global duopoly
    "APD",   # Air Products — industrial gases
    "ECL",   # Ecolab — water/hygiene chemicals
    "SHW",   # Sherwin-Williams — paint/coatings

    # ── Materials — expanded (8 new) ──────────────────────────────────────────
    "NEM",   # Newmont — gold mining, counter-cyclical
    "FCX",   # Freeport-McMoRan — copper, EV/infra demand
    "PPG",   # PPG Industries — coatings, auto/industrial
    "ALB",   # Albemarle — lithium, EV battery materials
    "DD",    # DuPont — specialty chemicals, electronics
    "LYB",   # LyondellBasell — plastics/chemicals, high yield
    "CE",    # Celanese — specialty chemicals
    "IFF",   # International Flavors & Fragrances — ingredients
]

# ── High-growth / high-volume additions (2026-08-28) ───────────────────────────
# Explicit user request, after liking a real US Reversion trade (CRWD): "expand
# stock universe but only with high growth volume share, good companies so we
# catch more these signals." Deliberately kept SEPARATE from SP500_TICKERS above
# (not all of these are confirmed current S&P 500 constituents -- some are
# Nasdaq-100/mid-cap growth names not yet index-eligible) rather than folding
# them in and making that name inaccurate. Run lookup_missing.py after this
# change too, same as any SP500_TICKERS edit, to fill in real Saxo UICs.
HIGH_GROWTH_TICKERS = [
    # ── Fintech / payments growth ──────────────────────────────────────────
    # (Block/Cash App already covered above as "XYZ" -- discovered while
    # building this list that a wrong-ticker "SQ" addition here would have
    # duplicated it AND resolved to a completely unrelated instrument, see
    # this file's git history/commit message for the full story.)
    "COIN",  # Coinbase — largest US crypto exchange
    "AFRM",  # Affirm — buy-now-pay-later
    "UPST",  # Upstart — AI lending

    # ── AI / compute infrastructure ────────────────────────────────────────
    "SMCI",  # Super Micro Computer — AI server hardware
    "ARM",   # Arm Holdings — chip IP powering most mobile/AI silicon
    "APP",   # AppLovin — mobile ad-tech, AI-driven ad engine

    # ── Space / quantum ─────────────────────────────────────────────────────
    "IONQ",  # IonQ — quantum computing
    "RKLB",  # Rocket Lab — small-satellite launch

    # ── Auto / retail growth ────────────────────────────────────────────────
    "CVNA",  # Carvana — online used-car retail, high-momentum turnaround

    # ── Social / consumer internet ──────────────────────────────────────────
    "RDDT",  # Reddit — social media, 2024 IPO
    "PINS",  # Pinterest — visual discovery/ads

    # ── Crypto-treasury / miners ─────────────────────────────────────────────
    "MSTR",  # Strategy (MicroStrategy) — largest corporate bitcoin holder
    "MARA",  # Marathon Digital — bitcoin mining
    "RIOT",  # Riot Platforms — bitcoin mining

    # ── Security / govtech ───────────────────────────────────────────────────
    "AXON",  # Axon Enterprise — tasers, police body-cams/software

    # ── Power / AI-datacenter demand theme ───────────────────────────────────
    "GEV",   # GE Vernova — power generation/grid, AI-datacenter demand
    "VST",   # Vistra — power generator, nuclear + AI-datacenter demand
    "CEG",   # Constellation Energy — largest US nuclear operator
    "NRG",   # NRG Energy — power generation/retail
    "TLN",   # Talen Energy — nuclear power, AI-datacenter PPAs

    # ── Consumer / retail high-growth ────────────────────────────────────────
    "ELF",   # e.l.f. Beauty — high-growth mass cosmetics
    "DECK",  # Deckers Brands — HOKA/UGG footwear
    "WSM",   # Williams-Sonoma — premium home goods
    "CROX",  # Crocs — footwear, high-margin growth

    # ── Biotech high-growth ───────────────────────────────────────────────────
    "ALNY",  # Alnylam Pharmaceuticals — RNAi therapeutics

    # ── Swing-fitness additions (2026-09-04) ─────────────────────────────────
    # Universe audit: removed ~34 low-beta / thin-volume names (utilities,
    # ultra-defensive staples, thin China ADRs, low-liquidity niche). Added
    # high-ADV momentum names with clear trend structure and catalyst calendars.
    "VRT",   # Vertiv Holdings — data-center cooling/power; AI-capex demand, β≈1.8
    "CAVA",  # CAVA Group — high-momentum fast-casual; IPO 2023, strong ROC signals
    "HIMS",  # Hims & Hers Health — telehealth/GLP-1 adjacent; β≈1.8, large ADV
    "TOST",  # Toast Inc. — restaurant POS/fintech; high-growth, momentum swings
    "ALAB",  # Astera Labs — AI connectivity chips (PCIe/CXL); rides NVDA cycle
    "CRDO",  # Credo Technology — AI networking semiconductor; hyperscaler capex play
    "DUOL",  # Duolingo — EdTech; β≈1.5, daily range >2%, clear earnings momentum
    "NCLH",  # Norwegian Cruise Line — high-beta travel; RSI-reversal + momentum
    "CART",  # Instacart (Maplebear) — grocery delivery; β>1.5, 2023 IPO
    "NBIS",  # Nebius Group — European AI cloud; extreme momentum, high ADV
]

# ── Nasdaq-100 + Dow Jones gap-fill (2026-09-03) ──────────────────────────────
# Explicit user request: "add the missing nasdaq-100 names plus DOW" so the US
# equity universe spans all three major US indices. These were the Nasdaq-100
# constituents (verified against the current index, 2026) NOT already present in
# SP500_TICKERS / HIGH_GROWTH_TICKERS above, plus DOW Inc (Dow-30 member).
# Deliberately kept as its own list -- most are quality compounders, not the
# mega-cap/high-momentum profile of the two lists above, and keeping the name
# accurate matters. Skipped on purpose:
#   GOOG  -- dual-class duplicate of GOOGL (already in); holding both would
#            double-weight Alphabet in a rank-weighted sleeve.
#   EA    -- went private 4 Aug 2026 (PIF/Silver Lake/Affinity, $55B LBO);
#            delisted, no longer a Nasdaq-100 member.
# Run lookup_missing.py (SIM UICs) AND lookup_instruments_live.py (LIVE UICs)
# after this change, same as any SP500_TICKERS edit.
NASDAQ100_DOW_TICKERS = [
    "ADP",   # Automatic Data Processing — payroll/HR software
    "CTAS",  # Cintas — uniform rental & facility services
    "MNST",  # Monster Beverage — energy drinks
    "ROP",   # Roper Technologies — diversified software/instruments (Nasdaq-listed)
    "PCAR",  # PACCAR — heavy-truck manufacturer (Kenworth/Peterbilt/DAF)
    "PAYX",  # Paychex — payroll/HR for SMBs
    "FAST",  # Fastenal — industrial & construction supply distribution
    "ODFL",  # Old Dominion Freight Line — less-than-truckload freight
    "CPRT",  # Copart — online salvage-vehicle auctions
    "CSGP",  # CoStar Group — commercial real-estate data/marketplaces
    "VRSK",  # Verisk Analytics — insurance/risk data analytics
    "CCEP",  # Coca-Cola Europacific Partners — largest Coke bottler (Nasdaq-listed)
    "FANG",  # Diamondback Energy — Permian Basin oil & gas
    "CDW",   # CDW Corp — IT hardware/software solutions reseller
    "GFS",   # GlobalFoundries — contract semiconductor manufacturing
    "TRI",   # Thomson Reuters — professional information/legal/tax data
    "DOW",   # Dow Inc — commodity & specialty chemicals (Dow-30, NYSE-listed)
]

# Deduplicated, sector order preserved
US_TICKERS = list(dict.fromkeys(SP500_TICKERS + HIGH_GROWTH_TICKERS + NASDAQ100_DOW_TICKERS))

# ── LIVE vs SIM split (2026-09-04) ───────────────────────────────────────────
# LIVE (atos_live_stocks.py / Saxo real-money):  LIVE_TICKERS only  (~337 names)
# SIM  (atos_runner.py / run_ibkr_stocks.py):    US_TICKERS in full (~398 names)
#
# SIM_ONLY_TICKERS = names excluded from LIVE entry scanning:
#   - Regional banks: rate-driven moves, thin LIVE sizing
#   - China ADRs: regulatory gap risk + IBKR LIVE data quality
#   - Speculative early-stage biotech: extreme overnight gap risk
#   - Low-ADV small-cap: thin enough that LIVE fills are unreliable
#   - Near-distressed names: wide spreads, execution risk on LIVE
#   - New speculative additions: need SIM track record before LIVE
SIM_ONLY_TICKERS: frozenset = frozenset({
    # Regional banks — rate-driven not earnings-driven, thin swing widths
    "FITB", "RF", "KEY", "HBAN", "MTB", "CFG", "ZION", "USB", "TFC", "PNC",
    # Marginal utilities (kept in SIM for RSI-reversal signals)
    "PCG", "SRE",
    # Speculative early-stage biotech — extreme gap risk, thin vol
    "CRSP", "EDIT", "NTLA", "NVCR", "RGEN",
    # China ADRs — regulatory gap risk, IBKR LIVE market-data reliability
    "BIDU", "NTES", "JD", "PDD", "SE", "BABA",
    # Small consumer discretionary — ADV < 2M, thin swings
    "VFC", "LEVI", "HOG", "HAS", "MAT", "AN",
    # Small industrials — ADV < 2M
    "J", "ACM", "TEX", "MAS", "EXP",
    # Thin medical devices & diagnostics — ADV < 1M each
    "PEN", "BAX", "ZBH", "LH", "DGX", "IDXX",
    # Low-price / near-distressed / speculative EV
    "LCID", "RIVN", "JBLU", "ALK",
    # Thin niche tech
    "PUBM", "DOCN",
    # Quantum / small-satellite launch — speculative, thin fills
    "IONQ", "RKLB",
    # New additions: need SIM track record before LIVE promotion
    "NBIS", "CART",
    # Low-ADV financials (< 1M shares/day)
    "MKTX", "NTRS", "WU", "GPN", "BR",
    # Marginal insurance — lower ADV than sector peers
    "AFL", "UNM",
    # Single-name niche exclusions
    "CHTR",   # Charter: ~700k ADV
    "NVR",    # NVR: ~$7k/share, ~20k shares/day
    # Small materials — thin fills
    "IFF", "CE",
})

LIVE_TICKERS: list = [t for t in US_TICKERS if t not in SIM_ONLY_TICKERS]

# ── Legacy / inactive ─────────────────────────────────────────────────────────
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
