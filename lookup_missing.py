"""
lookup_missing.py — fetch Saxo UICs for universe tickers not yet in instrument_map.csv
"""
import csv, time, sys, os, requests
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from saxo_client import find_instrument
from atos.universe import ATOS_UNIVERSE
from instrument_map import load_instrument_map

MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "instrument_map.csv")
US_EXCHANGES = {"NYSE", "NASDAQ", "XNYS", "XNAS", "BATS", "CBOE", "NYSE American", "AMEX"}


def best_us(matches, ticker):
    if not matches:
        return None
    for m in matches:
        sym  = (m.get("Symbol") or "").upper().split(":")[0]
        exch = m.get("ExchangeId", "")
        if sym == ticker.upper() and exch in US_EXCHANGES:
            return m
    for m in matches:
        if m.get("ExchangeId", "") in US_EXCHANGES:
            return m
    return matches[0]


def find_retry(ticker, retries=4):
    delay = 5
    for _ in range(retries):
        try:
            return find_instrument(ticker)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                print(f"  429 — waiting {delay}s...")
                time.sleep(delay); delay *= 2
            else:
                raise
    return find_instrument(ticker)


def main():
    try:
        imap = load_instrument_map()
    except Exception:
        imap = {}
    missing = [t for t in ATOS_UNIVERSE if t not in imap]
    print(f"Looking up {len(missing)} missing tickers\n")

    fieldnames = ["yahoo_ticker", "uic", "symbol", "exchange", "currency", "needs_review"]

    with open(MAP_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        found = 0
        for ticker in missing:
            try:
                matches = find_retry(ticker)
            except Exception as e:
                print(f"  ERROR {ticker}: {e}")
                writer.writerow({"yahoo_ticker": ticker, "uic": "", "symbol": "",
                                 "exchange": "", "currency": "", "needs_review": "yes - error"})
                f.flush(); time.sleep(2); continue

            if not matches:
                print(f"  NOT FOUND: {ticker}")
                writer.writerow({"yahoo_ticker": ticker, "uic": "", "symbol": "",
                                 "exchange": "", "currency": "", "needs_review": "yes - not found"})
                f.flush(); time.sleep(2); continue

            best = best_us(matches, ticker)
            sym_root = (best.get("Symbol") or "").upper().split(":")[0]
            flag = "" if sym_root == ticker.upper() else f"yes - got {best.get('Symbol')}"
            writer.writerow({
                "yahoo_ticker": ticker,
                "uic":          best.get("Identifier"),
                "symbol":       best.get("Symbol"),
                "exchange":     best.get("ExchangeId"),
                "currency":     best.get("CurrencyCode"),
                "needs_review": flag,
            })
            f.flush()
            found += 1
            tag = "  <-- REVIEW" if flag else ""
            print(f"  {ticker} -> UIC {best.get('Identifier')} ({best.get('Symbol')}, {best.get('ExchangeId')}){tag}")
            time.sleep(2)

    print(f"\nDone. {found}/{len(missing)} mapped. Appended to {MAP_FILE}")


if __name__ == "__main__":
    main()
