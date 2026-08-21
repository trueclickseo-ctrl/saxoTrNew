"""
resolve_etf_universe_ibkr.py
-------------------------------
Handles the ETF universe separately from lookup_instruments_ibkr.py
because it's a genuinely different problem, not just a bigger version of
the same lookup.

WHY THIS NEEDS A DIFFERENT APPROACH
--------------------------------------
saxo_etf_strategy/core/etf_universe.py builds its universe by *querying
Saxo's own catalog* -- paging through /ref/v1/instruments?AssetTypes=Etf
across every exchange. Your cached result
(saxo_etf_strategy/data/etf_universe.json) currently holds 8,924
instruments, mostly European UCITS ETFs on exchanges like XETR.

IBKR's TWS API has no equivalent "list every ETF you offer" endpoint --
reqContractDetails / qualifyContracts only resolve instruments you already
know the symbol for. There's nothing to page through. So this can't be
ported as "the same discovery logic, pointed at a different broker" --
the discovery *mechanism itself* doesn't exist on IBKR's side.

Also worth knowing before resolving thousands of these: most of that
8,924-instrument list is European retail UCITS ETFs (Xetra-listed, EUR-
denominated) which IBKR either doesn't offer, lists under a different
symbol, or requires a separate (often paid) market-data subscription for
in your account -- attempting to brute-force-resolve all 8,924 would be
slow, would hit IBKR's pacing limits hard, and a large fraction would
fail or resolve wrong regardless.

THE PRACTICAL PATH THIS SCRIPT TAKES
----------------------------------------
Reuse your existing Saxo-discovered universe as the *candidate list*
(it's already a curated, real set of tradable ETFs -- no need to
rediscover it from scratch), then resolve only a filtered subset against
IBKR -- by currency, by a symbol allow-list, or a row limit -- rather than
attempting all 8,924 blind. Run it with an explicit filter; it deliberately
refuses to run unfiltered so it can't accidentally kick off an
hours-long, mostly-failing scan.

Usage
-----
    # Resolve only USD-denominated ETFs from the cached universe:
    python resolve_etf_universe_ibkr.py --currency USD

    # Resolve a specific symbol list (e.g. ones you already know you trade):
    python resolve_etf_universe_ibkr.py --symbols SPY,QQQ,VTI,IEFA

    # Resolve the first N as a quick test:
    python resolve_etf_universe_ibkr.py --currency USD --limit 25
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time

import ibkr_client

CACHE_PATH = os.path.join("saxo_etf_strategy", "data", "etf_universe.json")


def load_candidates(currency: str | None, symbols: list[str] | None) -> list[dict]:
    with open(CACHE_PATH) as f:
        cache = json.load(f)
    instruments = cache["instruments"]

    if symbols:
        wanted = {s.upper() for s in symbols}
        return [i for i in instruments if i.get("Symbol", "").split(":")[0].upper() in wanted]

    if currency:
        return [i for i in instruments if i.get("CurrencyCode") == currency]

    return []   # unfiltered runs are refused, see main()


def resolve(candidates: list[dict], limit: int | None) -> list[dict]:
    if limit:
        candidates = candidates[:limit]

    rows = []
    for inst in candidates:
        # Saxo symbols look like "IBC5:xetr" -- strip the exchange suffix
        # and let IBKR's own SMART/exchange resolution take it from there.
        raw_symbol = inst.get("Symbol", "").split(":")[0]
        currency = inst.get("CurrencyCode", os.environ.get("IBKR_CURRENCY", "USD"))

        matches = ibkr_client.find_instrument(raw_symbol, asset_type="Etf")
        needs_review = "" if matches else "yes - not found on IBKR (may not be offered, or listed under a different symbol)"
        best = matches[0] if matches else {}

        rows.append({
            "saxo_symbol": inst.get("Symbol", ""),
            "description": inst.get("Description", ""),
            "uic": best.get("Uic", ""),
            "ibkr_symbol": best.get("Symbol", raw_symbol),
            "exchange": best.get("ExchangeId", ""),
            "currency": best.get("CurrencyCode", currency),
            "needs_review": needs_review,
        })
        flag = "  <-- REVIEW THIS" if needs_review else ""
        print(f"  {raw_symbol} -> conId {best.get('Uic', '?')}{flag}")
        time.sleep(0.5)

    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--currency", help="Only resolve ETFs in this currency, e.g. USD")
    parser.add_argument("--symbols", help="Comma-separated symbol allow-list, e.g. SPY,QQQ,VTI")
    parser.add_argument("--limit", type=int, help="Cap the number resolved (good for a first test run)")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    candidates = load_candidates(args.currency, symbols)

    if not candidates:
        print(
            "No filter matched, or none given. This script refuses to resolve "
            "all 8,924 cached instruments unfiltered -- pass --currency, "
            "--symbols, or both. See the script docstring for why."
        )
        return

    print(f"Resolving {len(candidates)} candidate ETF(s) against IBKR...")
    rows = resolve(candidates, args.limit)

    os.makedirs("data", exist_ok=True)
    out_path = "data/etf_map_ibkr.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["saxo_symbol", "description", "uic", "ibkr_symbol", "exchange", "currency", "needs_review"]
        )
        writer.writeheader()
        writer.writerows(rows)

    found = sum(1 for r in rows if not r["needs_review"])
    print(f"\nSaved: {out_path}  ({found}/{len(rows)} resolved)")


if __name__ == "__main__":
    main()
