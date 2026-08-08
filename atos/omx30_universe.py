"""
atos/omx30_universe.py
-----------------------
Canonical OMX30 universe for ATOS strategies.

All 30 tickers are the current OMX Stockholm 30 constituents, using
their yfinance (Yahoo Finance) symbol with the .ST suffix.

These are the same tickers already mapped in instrument_map.csv — this
module is the single source of truth for the strategy layer. If a stock
leaves or enters the index, update OMX30_TICKERS here.

Currency is always SEK — no FX conversion needed for this universe.
"""

OMX30_TICKERS: list[str] = [
    # Banks & financials
    "SEB-A.ST",
    "SWED-A.ST",
    "SHB-A.ST",
    "NDA-SE.ST",      # Nordea

    # Industrials
    "VOLV-B.ST",      # Volvo
    "ATCO-A.ST",      # Atlas Copco A
    "ATCO-B.ST",      # Atlas Copco B
    "SAND.ST",        # Sandvik
    "ALFA.ST",        # Alfa Laval
    "SKF-B.ST",       # SKF
    "SKA-B.ST",       # Skanska
    "HEXA-B.ST",      # Hexagon
    "SAAB-B.ST",      # SAAB

    # Tech & telecom
    "ERIC-B.ST",      # Ericsson
    "SINCH.ST",       # Sinch
    "TEL2-B.ST",      # Tele2
    "TELIA.ST",       # Telia

    # Consumer
    "HM-B.ST",        # H&M
    "ELUX-B.ST",      # Electrolux
    "GETI-B.ST",      # Getinge

    # Healthcare & pharma
    "AZN.ST",         # AstraZeneca (Stockholm listing)
    "ESSITY-B.ST",    # Essity
    "NIBE-B.ST",      # NIBE Industrier

    # Materials & energy
    "BOL.ST",         # Boliden
    "SCA-B.ST",       # SCA

    # Holding companies
    "INVE-B.ST",      # Investor
    "ABB.ST",         # ABB (Stockholm listing)
    "ASSA-B.ST",      # Assa Abloy
    "EVO.ST",         # Evolution Gaming

    # Real estate (included in OMX30 index)
    "SBB-B.ST",       # Samhällsbyggnadsbolaget
]

# Benchmark for the market risk-off check (OMX30 index via yfinance)
OMX30_BENCHMARK = "^OMX"
