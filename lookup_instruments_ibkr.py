"""
lookup_instruments_ibkr.py
----------------------------
IBKR equivalent of lookup_instruments.py -- one-time step that resolves
your ticker universes to IBKR conIds and writes CSVs shaped like
data/instrument_map.csv, so downstream code changes minimally.

Covers three of your four universes directly (see saxo_etf_strategy note
at the bottom of this file's docstring for why ETFs are handled
separately, in resolve_etf_universe_ibkr.py):

  1. Stocks  (config.ACTIVE_UNIVERSE)      -> data/instrument_map_ibkr.csv
  2. Forex   (forex/universe.py PAIRS)      -> data/forex_map_ibkr.csv
  3. Futures (futures/universe.py MARKETS,   -> data/futures_map_ibkr.csv
              ContractFutures entries only -- see WHAT'S SKIPPED below)

WHAT NEEDS MANUAL REVIEW, AND WHY
-------------------------------------
- Every futures row: `find_instrument()` resolves symbol+exchange but not
  a specific contract month/expiry, so each row is flagged for a manual
  check regardless of whether it resolved.
- ES/NQ/YM/DAX/HK50/GC/SI specifically: Saxo trades these as continuous
  index-CFDs or continuous spot-like instruments (no expiry). Their IBKR
  equivalents are standard expiring futures contracts on the same ticker
  (CME/CBOT/EUREX/HKFE/COMEX) -- same underlying exposure, but roll risk
  and contract sizing genuinely differ. Resolved, not skipped, but flagged.
- Run `python -m ibkr_client` (dry `test_connection()`) first to confirm
  IB Gateway is reachable before running this -- same prerequisite as
  running saxo_client.py's self-test before the original script.
"""

from __future__ import annotations

import csv
import os
import time

import config
import ibkr_client

# Yahoo-style suffix -> IBKR exchange code. These are IBKR's own codes,
# NOT Saxo's (e.g. Saxo's Stockholm ExchangeId is "SSE"; IBKR's is "SFB").
# Verify against TWS's contract search before trusting a new suffix here --
# same "LOW CONFIDENCE, verify" caveat the original script carries for its
# less-common exchanges.
EXCHANGE_HINT = {
    ".ST": ("SFB", "SEK"),     # Stockholm
    ".CO": ("CPH", "DKK"),     # Copenhagen
    ".DE": ("IBIS", "EUR"),    # Germany / Xetra
    ".L":  ("LSE", "GBP"),     # London
    ".PA": ("SBF", "EUR"),     # Paris
    ".AS": ("AEB", "EUR"),     # Amsterdam
    ".SW": ("EBS", "CHF"),     # Swiss (SIX)
    ".TO": ("TSE", "CAD"),     # Toronto  -- NOTE: IBKR's "TSE" is Toronto,
                                # not Tokyo; Saxo's lookup_instruments.py
                                # uses "TSE" for Tokyo -- this is a real
                                # collision, double-check any .T tickers by hand.
}


def strip_suffix(ticker: str) -> tuple[str, str | None, str | None]:
    for suffix, (exchange, currency) in EXCHANGE_HINT.items():
        if ticker.endswith(suffix):
            return ticker[: -len(suffix)], exchange, currency
    return ticker, "SMART", None   # plain US ticker -> SMART routing


def lookup_stocks() -> None:
    rows = []
    for ticker in config.ACTIVE_UNIVERSE:
        search_term, exchange, currency = strip_suffix(ticker)
        currency = currency or os.environ.get("IBKR_CURRENCY", "USD")

        symbol_str = f"{search_term}:STK:{exchange}:{currency}"
        matches = ibkr_client.find_instrument(symbol_str, asset_type="Stock")

        needs_review = "" if matches else "yes - not found, verify ticker/exchange by hand"
        best = matches[0] if matches else {}

        rows.append({
            "yahoo_ticker": ticker,
            "uic": best.get("Uic", ""),
            "symbol": best.get("Symbol", search_term),
            "exchange": best.get("ExchangeId", exchange),
            "currency": best.get("CurrencyCode", currency),
            "needs_review": needs_review,
        })
        flag = "  <-- REVIEW THIS" if needs_review else ""
        print(f"  {ticker} -> conId {best.get('Uic', '?')} "
              f"({best.get('Symbol', search_term)}, {exchange}){flag}")
        time.sleep(0.5)   # be gentle with IBKR's pacing limits

    os.makedirs("data", exist_ok=True)
    with open("data/instrument_map_ibkr.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["yahoo_ticker", "uic", "symbol", "exchange", "currency", "needs_review"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print("\nSaved: data/instrument_map_ibkr.csv")
    print("IMPORTANT: same as the Saxo version -- open this file and verify")
    print("each match, especially anything flagged needs_review, before")
    print("using it for real order placement.")


def lookup_forex() -> None:
    from forex.universe import PAIRS

    rows = []
    for pair in PAIRS:
        symbol = pair["symbol"]
        matches = ibkr_client.find_instrument(symbol, asset_type="FxSpot")
        needs_review = "" if matches else "yes - not found on IDEALPRO"
        best = matches[0] if matches else {}
        rows.append({
            "symbol": symbol,
            "uic": best.get("Uic", ""),
            "base": pair.get("base", ""),
            "quote": pair.get("quote", ""),
            "min_units": pair.get("min_units", ""),
            "needs_review": needs_review,
        })
        flag = "  <-- REVIEW THIS" if needs_review else ""
        print(f"  {symbol} -> conId {best.get('Uic', '?')}{flag}")
        time.sleep(0.5)

    os.makedirs("data", exist_ok=True)
    with open("data/forex_map_ibkr.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["symbol", "uic", "base", "quote", "min_units", "needs_review"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print("\nSaved: data/forex_map_ibkr.csv")


def lookup_futures() -> None:
    from futures.universe import MARKETS

    # Real CME-group / EUREX / HKFE exchange codes for each symbol. Saxo
    # wraps ES/NQ/YM/DAX/HK50 as continuous index CFDs and GC/SI as
    # continuous spot-like FxSpot -- IBKR has no equivalent continuous
    # product for any of these, but every one of these symbols IS a real,
    # standard futures contract on IBKR (same ticker, different product
    # type: expiring futures, not a continuous CFD/spot wrapper). That's
    # a real behavioural difference (roll risk, contract months) worth
    # having strategy code account for -- not a blocker to resolving them.
    FUTURES_EXCHANGE = {
        "ES": "CME", "NQ": "CME", "YM": "CBOT", "DAX": "EUREX", "HK50": "HKFE",
        "GC": "COMEX", "SI": "COMEX",
        "CL": "NYMEX", "NG": "NYMEX", "ZB": "CBOT", "ZC": "CBOT",
        "ZW": "CBOT", "ZS": "CBOT",
    }

    rows = []
    for m in MARKETS:
        symbol = m["symbol"]
        saxo_asset_type = m["asset_type"]
        exchange = FUTURES_EXCHANGE.get(symbol, os.environ.get("IBKR_FUT_EXCHANGE", "CME"))

        symbol_str = f"{symbol}:FUT:{exchange}:USD"
        matches = ibkr_client.find_instrument(symbol_str, asset_type="ContractFutures")

        # Every entry needs a human to pick/verify a contract month --
        # find_instrument() resolves the symbol+exchange but doesn't pin
        # an expiry, so IBKR may return the nearest contract by default
        # or nothing at all depending on ambiguity. Flag every row, not
        # just the misses, since "resolved" here doesn't mean "ready to
        # trade" the way it does for stocks/forex.
        if matches:
            needs_review = ("verify contract month -- resolved to nearest/default "
                             "expiry, confirm it's the one you want to trade")
        else:
            needs_review = "yes - not found, verify symbol/exchange and contract month by hand"
        best = matches[0] if matches else {}

        rows.append({
            "symbol": symbol,
            "uic": best.get("Uic", ""),
            "saxo_asset_type": saxo_asset_type,
            "ibkr_exchange": exchange,
            "needs_review": needs_review,
        })
        flag = "  <-- REVIEW: contract month" if matches else "  <-- NOT FOUND"
        print(f"  {symbol} ({saxo_asset_type} -> IBKR futures @ {exchange}) "
              f"-> conId {best.get('Uic', '?')}{flag}")
        time.sleep(0.5)

    os.makedirs("data", exist_ok=True)
    with open("data/futures_map_ibkr.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["symbol", "uic", "saxo_asset_type", "ibkr_exchange", "needs_review"]
        )
        writer.writeheader()
        writer.writerows(rows)
    print("\nSaved: data/futures_map_ibkr.csv")
    print("Every row needs a manual contract-month check before trading --")
    print("this script resolves symbol+exchange, not a specific expiry.")
    print("Also note: Saxo's ES/NQ/YM/DAX/HK50 are continuous index CFDs and")
    print("GC/SI are continuous spot-like -- their IBKR equivalents are")
    print("expiring futures contracts (roll risk applies, sizing may differ).")


if __name__ == "__main__":
    print("=== Stocks ===")
    lookup_stocks()
    print("\n=== Forex ===")
    lookup_forex()
    print("\n=== Futures ===")
    lookup_futures()
