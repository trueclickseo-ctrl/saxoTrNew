"""
config.py
---------
All the knobs you'll actually want to change live here, in one place.
You should never need to edit the other files to adjust how the bot behaves.
"""

# ── Universe ──────────────────────────────────────────────────────────────
# OMX30 tickers as they appear on Yahoo Finance (Stockholm exchange suffix .ST)
OMX30_TICKERS = [
    "ERIC-B.ST", "VOLV-B.ST", "ATCO-A.ST", "ATCO-B.ST", "INVE-B.ST",
    "SAND.ST", "SEB-A.ST", "SWED-A.ST", "SHB-A.ST", "HM-B.ST",
    "ABB.ST", "ALFA.ST", "ASSA-B.ST", "AZN.ST", "BOL.ST",
    "ELUX-B.ST", "ESSITY-B.ST", "EVO.ST", "GETI-B.ST", "HEXA-B.ST",
    "NDA-SE.ST", "NIBE-B.ST", "SBB-B.ST", "SCA-B.ST", "SINCH.ST",
    "SKA-B.ST", "SKF-B.ST", "TEL2-B.ST", "TELIA.ST", "SAAB-B.ST",
]

# US example universe (large, liquid names — swap/expand later)
US_TICKERS = ["AAPL", "MSFT", "AMZN", "GOOGL", "NVDA"]

# Copenhagen (OMXC25) — the 10 largest/most liquid names on Nasdaq Copenhagen,
# confirmed via TradingView Aug 2026. Full OMXC25 has 25 constituents; this is
# a "high trading volume" subset per your request, not the complete index —
# verify against the live list before assuming completeness.
COPENHAGEN_TICKERS = [
    "NOVO-B.CO",   # Novo Nordisk B
    "DSV.CO",      # DSV
    "DANSKE.CO",   # Danske Bank
    "MAERSK-B.CO", # A.P. Moller-Maersk B
    "MAERSK-A.CO", # A.P. Moller-Maersk A
    "ORSTED.CO",   # Orsted
    "NSIS-B.CO",   # Novonesis B (formerly Novozymes)
    "VWS.CO",      # Vestas Wind Systems
    "CARL-B.CO",   # Carlsberg B
    "GMAB.CO",     # Genmab
]

# Nasdaq-100 — top names by index weight (Slickcharts, Aug 2026), which
# tracks closely with trading volume for mega-caps. This is a liquid subset
# of the full 100, not the complete index — same caveat as above.
NASDAQ100_TICKERS = [
    "NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "AVGO", "META", "TSLA",
    "MU", "WMT", "AMD", "ASML", "CSCO", "INTC", "COST", "AMAT", "NFLX",
]

ACTIVE_UNIVERSE = OMX30_TICKERS + COPENHAGEN_TICKERS + NASDAQ100_TICKERS

# Germany (DAX) — large/liquid blue-chips from general knowledge, NOT
# confirmed against real trading-volume data the way Copenhagen/Nasdaq were
# (TradingView paywalled that data past the top few names). Treat this as
# best-effort; lookup_instruments.py's needs_review flags will catch any
# that don't map cleanly.
GERMANY_TICKERS = [
    "SAP.DE",    # SAP
    "SIE.DE",    # Siemens
    "ALV.DE",    # Allianz
    "AIR.DE",    # Airbus
    "VOW3.DE",   # Volkswagen (pref)
    "IFX.DE",    # Infineon
    "DBK.DE",    # Deutsche Bank
    "MBG.DE",    # Mercedes-Benz Group
    "BMW.DE",    # BMW
    "BAS.DE",    # BASF
    "ADS.DE",    # Adidas
    "DTE.DE",    # Deutsche Telekom
    "MUV2.DE",   # Munich Re
    "BAYN.DE",   # Bayer
    "RHM.DE",    # Rheinmetall
]

ACTIVE_UNIVERSE = ACTIVE_UNIVERSE + GERMANY_TICKERS

# NOTE: US-only was tried as an interim step (best win rate/avg P&L/trade in
# initial testing) but we're back to a full multi-market comparison now that
# realistic commissions are wired in, so all markets are compared fairly on
# the same fee model — see compare_markets.py after running main.py.

# UK (FTSE 100) — top 10 by ACTUAL today's trading volume, confirmed via
# LiveCharts UK volume-leader board (real volume numbers, not market cap).
UK_TICKERS = [
    "LLOY.L",  # Lloyds Banking Group
    "NWG.L",   # NatWest Group
    "BP.L",    # BP
    "VOD.L",   # Vodafone
    "BARC.L",  # Barclays
    "GLEN.L",  # Glencore
    "RTO.L",   # Rentokil Initial
    "HSBA.L",  # HSBC Holdings
    "SBRY.L",  # Sainsbury's
    "IAG.L",   # International Consolidated Airlines Group
]

# Full multi-market universe, now all under the SAME realistic commission
# model — this is the fair, apples-to-apples comparison the old numbers
# weren't. Canada/TSX intentionally left out for now: couldn't find a
# reliable current volume-ranked source (TSX volume pages were all
# JS-rendered, nothing safely extractable) — add properly later rather
# than guess.
ACTIVE_UNIVERSE = OMX30_TICKERS + COPENHAGEN_TICKERS + NASDAQ100_TICKERS + GERMANY_TICKERS + UK_TICKERS

# The five markets below are general-knowledge large/liquid blue-chips —
# NOT confirmed against real trading-volume data (couldn't find a reliable
# ranked-volume source for any of these five via search; pages were
# paywalled or JS-rendered). Same caveat tier as Germany, weaker than
# UK/Copenhagen/Nasdaq which WERE confirmed against real volume data.
# Verify via lookup_instruments.py's needs_review flags before trusting.

FRANCE_TICKERS = [
    "MC.PA",   # LVMH
    "TTE.PA",  # TotalEnergies
    "SAN.PA",  # Sanofi
    "OR.PA",   # L'Oreal
    "AI.PA",   # Air Liquide
    "SU.PA",   # Schneider Electric
    "BNP.PA",  # BNP Paribas
    "CS.PA",   # AXA
    "DG.PA",   # Vinci
    "BN.PA",   # Danone
]

NETHERLANDS_TICKERS = [
    "ASML.AS",  # ASML (note: separate from the US-listed ASML in NASDAQ100_TICKERS)
    "INGA.AS",  # ING Group
    "UNA.AS",   # Unilever
    "HEIA.AS",  # Heineken
    "PHIA.AS",  # Philips
    "AD.AS",    # Ahold Delhaize
    "ADYEN.AS", # Adyen
    "PRX.AS",   # Prosus
]

SWITZERLAND_TICKERS = [
    "NESN.SW",  # Nestle
    "NOVN.SW",  # Novartis
    "ROG.SW",   # Roche
    "UBSG.SW",  # UBS
    "ZURN.SW",  # Zurich Insurance
    "CFR.SW",   # Richemont
    "ABBN.SW",  # ABB (note: separate from ABB.ST already in OMX30_TICKERS — same company, dual-listed)
    "SCMN.SW",  # Swisscom
]

CANADA_TICKERS = [
    "RY.TO",   # Royal Bank of Canada
    "TD.TO",   # Toronto-Dominion Bank
    "ENB.TO",  # Enbridge
    "CNQ.TO",  # Canadian Natural Resources
    "BNS.TO",  # Bank of Nova Scotia
    "SU.TO",   # Suncor Energy (note: different company from SU.PA/Schneider above, same 2-letter code, different exchange)
    "CP.TO",   # Canadian Pacific Kansas City
    "BN.TO",   # Brookfield (note: different company from BN.PA/Danone, same code, different exchange)
]

JAPAN_TICKERS = [
    "7203.T",  # Toyota Motor
    "6758.T",  # Sony Group
    "8306.T",  # Mitsubishi UFJ Financial
    "9984.T",  # SoftBank Group
    "6861.T",  # Keyence
    "7974.T",  # Nintendo
    "9983.T",  # Fast Retailing (Uniqlo)
    "8035.T",  # Tokyo Electron
]

ACTIVE_UNIVERSE = (ACTIVE_UNIVERSE + FRANCE_TICKERS + NETHERLANDS_TICKERS
                    + SWITZERLAND_TICKERS + CANADA_TICKERS + JAPAN_TICKERS)

# ── Index universe (CFDs) ───────────────────────────────────────────────────
# Yahoo Finance tickers used ONLY for backtesting price history — these are
# NOT Saxo instruments. The actual Saxo Uics for trading live/paper are in
# data/index_map.csv (built by lookup_indices.py). Label here matches the
# 'label' column in that file so the two can be joined later.
INDEX_TICKERS = {
    "DAX_DE":        "^GDAXI",   # Germany 40
    "COPENHAGEN_25": "^OMXC25",  # Denmark 25
    "US_SP500":      "^GSPC",    # US 500
    "US_NASDAQ100":  "^IXIC",    # US Tech 100 (Nasdaq Composite as a proxy —
                                  # ^NDX is the pure Nasdaq-100 if you want an exact match)
    "US_DOW30":      "^DJI",     # US 30
}
ACTIVE_INDEX_UNIVERSE = list(INDEX_TICKERS.keys())

# ── CFD-specific parameters ─────────────────────────────────────────────────
# IMPORTANT — these are PLACEHOLDER ESTIMATES, not real Saxo figures. Margin
# requirements and financing rates vary by instrument and change over time.
# Before trusting any CFD backtest number, verify the real values for each
# index: log into SaxoTraderGO -> open the instrument -> "Key figures" /
# "Conditions" tab shows the actual margin %. Financing rates are published
# on Saxo's site under "Trading conditions" for each asset class.
# Until then, treat every CFD backtest result here as directional only.
CFD_MARGIN_RATE = 0.05             # placeholder: assume 5% margin requirement — VERIFY per index
CFD_ANNUAL_FINANCING_RATE = 0.065  # placeholder: assume ~6.5%/year on notional held overnight — VERIFY

# ── Strategy parameters (trend-following: moving average crossover) ───────
FAST_MA = 20          # fast moving average window (days)
SLOW_MA = 50           # slow moving average window (days)
ATR_PERIOD = 14        # Average True Range window, used for stops & sizing
ATR_STOP_MULTIPLE = 2.5   # stop-loss placed this many ATRs below entry (long only, to start)

# ── Risk management ────────────────────────────────────────────────────────
STARTING_CAPITAL = 10_000        # SEK (or whatever your paper/live account uses) — edit to match yours
RISK_PER_TRADE_PCT = 0.01        # risk max 1% of capital on any single trade
MAX_OPEN_POSITIONS = 5           # cap simultaneous positions so risk doesn't concentrate
MAX_DAILY_LOSS_PCT = 0.03        # circuit breaker: stop trading for the day if down 3%
COMMISSION_PCT = 0.0005          # legacy flat estimate — kept for reference, backtest.py now uses the model below
COMMISSION_RATE_PCT = 0.0008     # Saxo Classic tier US stocks: 0.08% of trade value — VERIFY against your actual tier
MIN_COMMISSION_USD = 1.00        # Saxo's minimum ticket fee per trade (Classic tier) — the real bite on small trades
# NOTE: Saxo also charges a separate custody fee on overnight stock holdings
# (tier-dependent %, not modeled here — check your account's actual rate).
# This does NOT apply to day trades closed same-day, but DOES apply to any
# swing position held overnight — a real cost difference between the two
# strategies not yet captured in this backtest.

# ── Backtest settings ──────────────────────────────────────────────────────
BACKTEST_START = "2018-01-01"
BACKTEST_END = None              # None = up to today
