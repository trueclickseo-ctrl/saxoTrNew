"""
avanza_sweep_backtest.py
------------------------
Batch backtest all candidate instruments from the Avanza "most liquid
mini futures / turbos" list.  Runs _simulate() from the existing engine
for each (underlying, direction, leverage, strategy) combination and prints
a ranked comparison table.

Usage:
    python avanza_sweep_backtest.py            # run all candidates
    python avanza_sweep_backtest.py --years 5  # 5-year lookback (default 5)
    python avanza_sweep_backtest.py --sort return  # sort by return% (default)
    python avanza_sweep_backtest.py --sort pf      # sort by profit factor

Columns:
    Underlying  Direction  Lev  Strategy   Trades  WR%  PF   Return%  MaxDD%  KOs
"""
from __future__ import annotations

import argparse
import sys
from typing import Optional

# Reuse the engine from the existing backtest file
from avanza_mini_futures_backtest import _download, _moving_average, _simulate, _stats

# ── Candidate list ────────────────────────────────────────────────────────────
#
# Each entry: (label, yahoo_ticker, direction, leverage, strategy, ma_days)
#
# Coverage from Avanza screenshot (2026-09-11):
#   OMX Stockholm 30  : MINI L/S (3-5x), TURBO L (47-75x)
#   Nasdaq 100        : TURBO L (61-79x), TURBO S (39-57x)
#   Oil / Olja        : TURBO L (7-17x), TURBO S / MFS (10-15x)
#   Gold SHORT        : TURBO S (38x)
#   DAX SHORT         : MINI S (21-28x)
#   SP500 SHORT       : MINI S (15x)
#
# We test at representative leverage buckets:
#   MINI class  (3-10x)  → test 5x and 10x
#   TURBO class (15-80x) → test 15x, 30x, 50x
# Both strategies (trend / reversion) for each new underlying.

CANDIDATES: list[tuple[str, str, str, float, str, int]] = [
    # label           ticker    direction   leverage  strategy     ma_days

    # ── OMX Stockholm 30 ─────────────────────────────────────────────────────
    ("MINI L OMX",    "^OMX",   "LONG",        5.0, "reversion",  20),
    ("MINI L OMX",    "^OMX",   "LONG",       10.0, "reversion",  20),
    ("MINI L OMX",    "^OMX",   "LONG",        5.0, "trend",      20),
    ("MINI L OMX",    "^OMX",   "LONG",       10.0, "trend",      20),
    ("MINI S OMX",    "^OMX",   "SHORT",       5.0, "reversion",  20),
    ("MINI S OMX",    "^OMX",   "SHORT",      10.0, "reversion",  20),
    ("MINI S OMX",    "^OMX",   "SHORT",       5.0, "trend",      20),
    ("TURBO L OMX",   "^OMX",   "LONG",       30.0, "trend",      20),
    ("TURBO L OMX",   "^OMX",   "LONG",       50.0, "trend",      20),
    ("TURBO L OMX",   "^OMX",   "LONG",       30.0, "reversion",  20),

    # ── Nasdaq 100 ────────────────────────────────────────────────────────────
    ("TURBO L NDX",   "^NDX",   "LONG",        5.0, "reversion",  20),
    ("TURBO L NDX",   "^NDX",   "LONG",       10.0, "reversion",  20),
    ("TURBO L NDX",   "^NDX",   "LONG",        5.0, "trend",      20),
    ("TURBO L NDX",   "^NDX",   "LONG",       10.0, "trend",      20),
    ("TURBO L NDX",   "^NDX",   "LONG",       30.0, "trend",      20),
    ("TURBO L NDX",   "^NDX",   "LONG",       50.0, "trend",      20),
    ("TURBO S NDX",   "^NDX",   "SHORT",       5.0, "reversion",  20),
    ("TURBO S NDX",   "^NDX",   "SHORT",      10.0, "reversion",  20),
    ("TURBO S NDX",   "^NDX",   "SHORT",      30.0, "reversion",  20),
    ("TURBO S NDX",   "^NDX",   "SHORT",      50.0, "reversion",  20),
    ("TURBO S NDX",   "^NDX",   "SHORT",       5.0, "trend",      20),
    ("TURBO S NDX",   "^NDX",   "SHORT",      10.0, "trend",      20),

    # ── Oil / Crude (CL=F) ────────────────────────────────────────────────────
    ("TURBO L OIL",   "CL=F",   "LONG",        5.0, "trend",      20),
    ("TURBO L OIL",   "CL=F",   "LONG",       10.0, "trend",      20),
    ("TURBO L OIL",   "CL=F",   "LONG",       15.0, "trend",      20),
    ("TURBO L OIL",   "CL=F",   "LONG",        5.0, "reversion",  20),
    ("TURBO L OIL",   "CL=F",   "LONG",       10.0, "reversion",  20),
    ("TURBO S OIL",   "CL=F",   "SHORT",       5.0, "trend",      20),
    ("TURBO S OIL",   "CL=F",   "SHORT",      10.0, "trend",      20),
    ("TURBO S OIL",   "CL=F",   "SHORT",      15.0, "trend",      20),
    ("TURBO S OIL",   "CL=F",   "SHORT",       5.0, "reversion",  20),
    ("TURBO S OIL",   "CL=F",   "SHORT",      10.0, "reversion",  20),

    # ── Gold SHORT (GC=F) — LONG already in paper portfolio ──────────────────
    ("TURBO S GOLD",  "GC=F",   "SHORT",       5.0, "trend",      20),
    ("TURBO S GOLD",  "GC=F",   "SHORT",      10.0, "trend",      20),
    ("TURBO S GOLD",  "GC=F",   "SHORT",      20.0, "trend",      20),
    ("TURBO S GOLD",  "GC=F",   "SHORT",      30.0, "trend",      20),
    ("TURBO S GOLD",  "GC=F",   "SHORT",       5.0, "reversion",  20),
    ("TURBO S GOLD",  "GC=F",   "SHORT",      10.0, "reversion",  20),

    # ── DAX SHORT ─────────────────────────────────────────────────────────────
    ("MINI S DAX",    "^GDAXI", "SHORT",       5.0, "reversion",  20),
    ("MINI S DAX",    "^GDAXI", "SHORT",      10.0, "reversion",  20),
    ("MINI S DAX",    "^GDAXI", "SHORT",      20.0, "reversion",  20),
    ("MINI S DAX",    "^GDAXI", "SHORT",      30.0, "reversion",  20),
    ("MINI S DAX",    "^GDAXI", "SHORT",       5.0, "trend",      20),
    ("MINI S DAX",    "^GDAXI", "SHORT",      10.0, "trend",      20),

    # ── SP500 SHORT ───────────────────────────────────────────────────────────
    ("MINI S SP500",  "^GSPC",  "SHORT",       5.0, "reversion",  20),
    ("MINI S SP500",  "^GSPC",  "SHORT",      10.0, "reversion",  20),
    ("MINI S SP500",  "^GSPC",  "SHORT",      15.0, "reversion",  20),
    ("MINI S SP500",  "^GSPC",  "SHORT",       5.0, "trend",      20),
    ("MINI S SP500",  "^GSPC",  "SHORT",      10.0, "trend",      20),
]

# ── Data cache (avoid re-downloading same ticker) ─────────────────────────────
_DATA_CACHE: dict[str, tuple] = {}

def _get_data(ticker: str, years: float):
    if ticker not in _DATA_CACHE:
        print(f"  Downloading {years:.0f}y of {ticker}...", end=" ", flush=True)
        closes, highs, lows, dates = _download(ticker, years)
        print(f"{len(closes)} bars ({dates[0]} → {dates[-1]})")
        _DATA_CACHE[ticker] = (closes, highs, lows, dates)
    return _DATA_CACHE[ticker]


# ── Main sweep ────────────────────────────────────────────────────────────────

def run_sweep(years: float = 5.0, sort_by: str = "return",
              budget_sek: float = 2000.0, financing_rate: float = 0.055
             ) -> list[dict]:
    results = []

    for label, ticker, direction, leverage, strategy, ma_days in CANDIDATES:
        closes, highs, lows, dates = _get_data(ticker, years)
        if len(closes) < ma_days + 10:
            continue

        ma = _moving_average(closes, ma_days)
        trades = _simulate(
            closes, highs, lows, dates, ma,
            direction      = direction,
            leverage       = leverage,
            financing_rate = financing_rate,
            budget_sek     = budget_sek,
            ma_days        = ma_days,
            strategy       = strategy,
        )
        s = _stats(trades, budget_sek)
        if not s:
            continue

        results.append({
            "label":     label,
            "ticker":    ticker,
            "direction": direction,
            "leverage":  leverage,
            "strategy":  strategy,
            "n_trades":  s["n_trades"],
            "wr":        s["win_rate"],
            "pf":        s["profit_factor"],
            "return":    s["return_pct"],
            "max_dd":    s["max_dd_pct"],
            "kos":       s["ko_count"],
            "avg_days":  s["avg_days"],
        })

    # Sort
    key_map = {"return": "return", "pf": "pf", "wr": "wr"}
    sort_key = key_map.get(sort_by, "return")
    results.sort(key=lambda r: r[sort_key], reverse=True)
    return results


def _print_table(results: list[dict], years: float) -> None:
    SEP = "─"

    # Header
    print(f"\n{'='*105}")
    print(f"  AVANZA NEW INSTRUMENTS SWEEP  —  {years:.0f}-year backtest  "
          f"(2,000 SEK budget, 5.5% financing, 20-day MA)")
    print(f"{'='*105}")
    hdr = (f"  {'Label':<16} {'Ticker':<8} {'Dir':<6} {'Lev':>5} "
           f"{'Strategy':<10} {'Trd':>4} {'WR%':>5} {'PF':>5} "
           f"{'Ret%':>7} {'MaxDD':>6} {'KOs':>4} {'AvgDays':>7}")
    print(hdr)
    print(f"  {SEP*101}")

    prev_ticker = None
    for r in results:
        # Separator between different underlyings
        if r["ticker"] != prev_ticker and prev_ticker is not None:
            print(f"  {SEP*101}")
        prev_ticker = r["ticker"]

        ret_str = f"{r['return']:+.0f}%"
        pf_str  = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "∞"
        ko_flag = " ⚠" if r["kos"] > 0 else ""
        # Colour-code return: positive = ✓, negative = ✗
        mark = "✓" if r["return"] > 0 else "✗"

        print(f"  {r['label']:<16} {r['ticker']:<8} {r['direction']:<6} "
              f"{r['leverage']:>4.0f}x {r['strategy']:<10} "
              f"{r['n_trades']:>4} {r['wr']:>5.1f} {pf_str:>5} "
              f"{ret_str:>7} {r['max_dd']:>5.1f}% {r['kos']:>4}{ko_flag:2s} "
              f"{r['avg_days']:>6.1f}d  {mark}")

    print(f"  {SEP*101}")

    # Top picks (return > 0, KOs == 0, PF >= 1.3)
    top = [r for r in results if r["return"] > 0 and r["kos"] == 0 and r["pf"] >= 1.3]
    if top:
        print(f"\n  TOP PICKS (positive return, 0 KOs, PF≥1.3)  — ranked by return%:\n")
        for i, r in enumerate(top[:10], 1):
            pf_str = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "∞"
            print(f"  {i:>2}. {r['label']:<16} {r['direction']:<6} {r['leverage']:>4.0f}x  "
                  f"{r['strategy']:<10}  WR={r['wr']:.0f}%  PF={pf_str}  "
                  f"Ret={r['return']:+.0f}%  DD={r['max_dd']:.0f}%  "
                  f"AvgHold={r['avg_days']:.0f}d")
    else:
        print("\n  No candidates passed all three filters (Ret>0, KOs=0, PF≥1.3).")

    print(f"\n  ⚠  High-leverage (≥30x) results are included for reference only.")
    print(f"  ⚠  KO risk at 30x = 3.3%, at 50x = 2.0% adverse move wipes position.")
    print(f"  ⚠  Backtest does NOT model roll costs when crude oil (CL=F) futures roll.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Avanza mini/turbo futures sweep backtest — new instrument candidates."
    )
    p.add_argument("--years",  type=float, default=5.0,
                   help="Backtest lookback in years (default 5)")
    p.add_argument("--sort",   default="return",
                   choices=["return", "pf", "wr"],
                   help="Sort column: return (default), pf, wr")
    p.add_argument("--budget", type=float, default=2000.0,
                   help="Starting capital per instrument in SEK (default 2000)")
    args = p.parse_args()

    print(f"\n  Running sweep backtest: {len(CANDIDATES)} candidate combinations "
          f"over {args.years:.0f} years...\n")

    results = run_sweep(years=args.years, sort_by=args.sort, budget_sek=args.budget)
    _print_table(results, args.years)


if __name__ == "__main__":
    main()
