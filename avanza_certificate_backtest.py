"""
avanza_certificate_backtest.py
--------------------------------
Backtest Avanza BULL/BEAR daily leverage certificates (DLCs).

Mechanics (different from mini futures):
  - Daily rebalancing: leverage resets to fixed multiplier every trading day
  - P&L each day = prev_value × (1 + leverage × daily_underlying_return)
  - Beta slippage: in volatile/sideways markets, daily compounding causes
    value decay even if underlying returns to entry level
  - No financing barrier — but value approaches zero through compounding losses
  - At leverage L: underlying move > 1/L in one day = total wipe

Comparison with mini futures (same underlying, same strategy):
  Mini futures  → financing level drifts over weeks, KO from sustained move
  Certificates  → can lose 100% in one bad day at 20x, beta slippage ongoing

Underlyings tested (new vs existing):
  Silver  SI=F/SLV   — BULL X5, X15; BEAR X5 (trend + reversion)
  Bitcoin BTC-USD    — BULL X1 tracker (trend)
  Ethereum ETH-USD   — BULL X1 tracker (trend)
  Oil     CL=F       — BULL X2/X5/X10/X16 (slippage progression demo)
  Gold    GC=F       — BULL X2/X5/X20 (slippage demo)
  OMX     ^OMX       — BULL X18/X20 (slippage demo)
  DAX     ^GDAXI     — BULL X20 (slippage demo)
  Nasdaq  ^NDX       — BEAR X20 (slippage demo)

Usage:
    python avanza_certificate_backtest.py
    python avanza_certificate_backtest.py --years 5 --sort return
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

# ── Universe ──────────────────────────────────────────────────────────────────

UNDERLYINGS = {
    "SI=F":    {"name": "Silver",        "ccy": "USD"},
    "SLV":     {"name": "Silver ETF",    "ccy": "USD"},
    "BTC-USD": {"name": "Bitcoin",       "ccy": "USD"},
    "ETH-USD": {"name": "Ethereum",      "ccy": "USD"},
    "CL=F":    {"name": "Crude Oil",     "ccy": "USD"},
    "GC=F":    {"name": "Gold",          "ccy": "USD"},
    "^OMX":    {"name": "OMXS30",        "ccy": "SEK"},
    "^GDAXI":  {"name": "DAX",           "ccy": "EUR"},
    "^NDX":    {"name": "Nasdaq-100",    "ccy": "USD"},
    "^GSPC":   {"name": "S&P 500",       "ccy": "USD"},
}

# ── Candidates ────────────────────────────────────────────────────────────────
#
# (label, ticker, direction, leverage, strategy, ma_days)
#
# Products from Avanza certificates list (2026-09-11):
#   BULL OLJA X2/X5/X10/X12/X15/X16  — Oil LONG
#   BEAR OLJA X5/X6/X8/X10/X12/X15/X16 — Oil SHORT
#   BULL GULD X2/X5/X20              — Gold LONG
#   BEAR GULD X2/X20                 — Gold SHORT
#   BULL OMX X18/X20                 — OMX LONG
#   BEAR OMX X5/X20                  — OMX SHORT
#   BULL DAX X20                     — DAX LONG
#   BEAR DAX X20                     — DAX SHORT
#   BULL NASDAQ / BEAR NASDAQ X20    — NDX
#   BULL SILVER X5/X15/X20           — Silver LONG (NEW)
#   BTC/ETH trackers X1              — Crypto trackers (NEW)

CANDIDATES: list[tuple[str, str, str, float, str, int]] = [
    # ── Silver (NEW) ─────────────────────────────────────────────────────────
    ("BULL SILVER X5",  "SLV",    "LONG",   5.0, "trend",      20),
    ("BULL SILVER X5",  "SLV",    "LONG",   5.0, "reversion",  20),
    ("BULL SILVER X15", "SLV",    "LONG",  15.0, "trend",      20),
    ("BULL SILVER X20", "SLV",    "LONG",  20.0, "trend",      20),
    ("BEAR SILVER X5",  "SLV",    "SHORT",  5.0, "trend",      20),

    # ── Bitcoin (NEW) — 1x tracker, pure crypto directional exposure ──────────
    ("BTC Tracker X1",  "BTC-USD","LONG",   1.0, "trend",      20),
    ("BTC Tracker X1",  "BTC-USD","LONG",   1.0, "reversion",  20),
    # Also test with low leverage certificates
    ("BULL BTC X2",     "BTC-USD","LONG",   2.0, "trend",      20),
    ("BULL BTC X3",     "BTC-USD","LONG",   3.0, "trend",      20),

    # ── Ethereum (NEW) — 1x tracker ───────────────────────────────────────────
    ("ETH Tracker X1",  "ETH-USD","LONG",   1.0, "trend",      20),
    ("ETH Tracker X1",  "ETH-USD","LONG",   1.0, "reversion",  20),
    ("BULL ETH X2",     "ETH-USD","LONG",   2.0, "trend",      20),
    ("BULL ETH X3",     "ETH-USD","LONG",   3.0, "trend",      20),

    # ── Oil — slippage progression (already backtested as mini futures) ───────
    ("BULL OLJA X2",    "CL=F",   "LONG",   2.0, "trend",      20),
    ("BULL OLJA X5",    "CL=F",   "LONG",   5.0, "trend",      20),
    ("BULL OLJA X10",   "CL=F",   "LONG",  10.0, "trend",      20),
    ("BULL OLJA X16",   "CL=F",   "LONG",  16.0, "trend",      20),
    ("BEAR OLJA X5",    "CL=F",   "SHORT",  5.0, "trend",      20),
    ("BEAR OLJA X10",   "CL=F",   "SHORT", 10.0, "trend",      20),
    ("BEAR OLJA X16",   "CL=F",   "SHORT", 16.0, "trend",      20),

    # ── Gold — slippage demo ──────────────────────────────────────────────────
    ("BULL GULD X2",    "GC=F",   "LONG",   2.0, "trend",      20),
    ("BULL GULD X5",    "GC=F",   "LONG",   5.0, "trend",      20),
    ("BULL GULD X20",   "GC=F",   "LONG",  20.0, "trend",      20),
    ("BEAR GULD X2",    "GC=F",   "SHORT",  2.0, "trend",      20),
    ("BEAR GULD X20",   "GC=F",   "SHORT", 20.0, "trend",      20),

    # ── OMX — slippage demo ───────────────────────────────────────────────────
    ("BULL OMX X5",     "^OMX",   "LONG",   5.0, "reversion",  20),
    ("BULL OMX X10",    "^OMX",   "LONG",  10.0, "reversion",  20),
    ("BULL OMX X18",    "^OMX",   "LONG",  18.0, "trend",      20),
    ("BULL OMX X20",    "^OMX",   "LONG",  20.0, "trend",      20),
    ("BEAR OMX X5",     "^OMX",   "SHORT",  5.0, "trend",      20),
    ("BEAR OMX X20",    "^OMX",   "SHORT", 20.0, "trend",      20),

    # ── DAX ───────────────────────────────────────────────────────────────────
    ("BULL DAX X5",     "^GDAXI", "LONG",   5.0, "reversion",  20),
    ("BULL DAX X20",    "^GDAXI", "LONG",  20.0, "trend",      20),
    ("BEAR DAX X20",    "^GDAXI", "SHORT", 20.0, "trend",      20),

    # ── Nasdaq ────────────────────────────────────────────────────────────────
    ("BULL NDX X5",     "^NDX",   "LONG",   5.0, "trend",      20),
    ("BULL NDX X20",    "^NDX",   "LONG",  20.0, "trend",      20),
    ("BEAR NDX X20",    "^NDX",   "SHORT", 20.0, "trend",      20),
]

# ── Data download ─────────────────────────────────────────────────────────────

_DATA_CACHE: dict[str, tuple] = {}

def _download(ticker: str, years: float):
    if ticker in _DATA_CACHE:
        return _DATA_CACHE[ticker]
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("pip install yfinance")
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=int(years * 365.25) + 60)
    df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"),
                     auto_adjust=True, progress=False)
    if df.empty:
        return [], [], []
    def _col(c):
        s = df[c]
        if hasattr(s, "squeeze"):
            s = s.squeeze()
        return [float(x) for x in s.tolist()]
    closes = _col("Close")
    dates  = [str(d.date()) for d in df.index.tolist()]
    _DATA_CACHE[ticker] = (closes, dates)
    return closes, dates


def _ma(closes: list[float], n: int) -> list[Optional[float]]:
    ma: list[Optional[float]] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        ma[i] = sum(closes[i - n + 1 : i + 1]) / n
    return ma


# ── Certificate simulation (daily rebalancing) ───────────────────────────────

@dataclass
class CTrade:
    entry_date:  str
    exit_date:   str  = ""
    direction:   str  = "LONG"
    entry_price: float = 0.0
    exit_price:  float = 0.0
    leverage:    float = 1.0
    budget_sek:  float = 2000.0
    pnl_sek:     float = 0.0
    days_held:   int   = 0
    beta_slippage_pct: float = 0.0  # theoretical naive return - actual return


def _simulate_cert(closes: list[float], dates: list[str],
                   ma_vals: list[Optional[float]],
                   direction: str, leverage: float, budget_sek: float,
                   ma_days: int, strategy: str = "trend") -> list[CTrade]:
    """
    Simulate daily-rebalanced certificate.
    Each day the cert value changes by: value × leverage × daily_underlying_return
    LONG : value *= (1 + lev × (S_i / S_{i-1} - 1))
    SHORT: value *= (1 - lev × (S_i / S_{i-1} - 1))
    Value is floored at zero (total loss).
    """
    trades: list[CTrade] = []
    in_position = False
    pos_value   = 0.0
    entry_price = 0.0
    entry_date  = ""
    days_held   = 0
    equity      = budget_sek
    rev         = (strategy == "reversion")

    for i in range(ma_days, len(closes)):
        S    = closes[i]
        S_p  = closes[i - 1]
        date = dates[i]
        ma_i = ma_vals[i]
        ma_p = ma_vals[i - 1]

        if ma_i is None or ma_p is None:
            continue

        if not in_position:
            if not rev:
                signal = (direction == "LONG"  and S_p <= ma_p and S > ma_i) or \
                         (direction == "SHORT" and S_p >= ma_p and S < ma_i)
            else:
                signal = (direction == "LONG"  and S_p >= ma_p and S < ma_i) or \
                         (direction == "SHORT" and S_p <= ma_p and S > ma_i)
            if signal:
                in_position = True
                pos_value   = equity
                entry_price = S
                entry_date  = date
                days_held   = 0
        else:
            days_held += 1
            # Daily rebalancing
            daily_ret = S / S_p - 1.0
            if direction == "LONG":
                pos_value *= (1.0 + leverage * daily_ret)
            else:
                pos_value *= (1.0 - leverage * daily_ret)
            pos_value = max(pos_value, 0.0)

            # Exit signal
            if not rev:
                exit_sig = (direction == "LONG"  and S < ma_i) or \
                           (direction == "SHORT" and S > ma_i)
            else:
                exit_sig = (direction == "LONG"  and S > ma_i) or \
                           (direction == "SHORT" and S < ma_i)

            last_bar = (i == len(closes) - 1)

            if exit_sig or last_bar or pos_value < equity * 0.001:
                # Theoretical naive return (no beta slippage)
                if direction == "LONG":
                    naive = equity * (1 + leverage * (S / entry_price - 1))
                else:
                    naive = equity * (1 - leverage * (S / entry_price - 1))
                naive = max(naive, 0.0)
                slippage_pct = (pos_value - naive) / equity * 100 if equity > 0 else 0.0

                pnl_sek = pos_value - equity
                equity  = max(equity + pnl_sek, 0.0)

                trades.append(CTrade(
                    entry_date       = entry_date,
                    exit_date        = date,
                    direction        = direction,
                    entry_price      = entry_price,
                    exit_price       = S,
                    leverage         = leverage,
                    budget_sek       = budget_sek,
                    pnl_sek          = round(pnl_sek, 2),
                    days_held        = days_held,
                    beta_slippage_pct= round(slippage_pct, 1),
                ))
                in_position = False

    return trades


def _stats(trades: list[CTrade], budget_sek: float) -> dict:
    if not trades:
        return {}
    pnls   = [t.pnl_sek for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    equity = budget_sek
    peak   = budget_sek
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_sek
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    pf = abs(sum(wins) / sum(losses)) if sum(losses) != 0 else float("inf")
    avg_slip = sum(t.beta_slippage_pct for t in trades) / len(trades)
    return {
        "n_trades":   len(trades),
        "win_rate":   len(wins) / len(trades) * 100,
        "total_pnl":  sum(pnls),
        "return_pct": sum(pnls) / budget_sek * 100,
        "profit_factor": pf,
        "max_dd_pct": max_dd,
        "avg_days":   sum(t.days_held for t in trades) / len(trades),
        "avg_slippage_pct": avg_slip,
    }


# ── Main sweep ────────────────────────────────────────────────────────────────

def run_sweep(years: float = 5.0, budget_sek: float = 2000.0) -> list[dict]:
    results = []
    for label, ticker, direction, leverage, strategy, ma_days in CANDIDATES:
        data = _download(ticker, years)
        if not data[0]:
            print(f"  WARNING: no data for {ticker}, skipping.")
            continue
        closes, dates = data
        if len(closes) < ma_days + 10:
            continue
        ma_vals = _ma(closes, ma_days)
        trades  = _simulate_cert(closes, dates, ma_vals, direction, leverage,
                                 budget_sek, ma_days, strategy)
        s = _stats(trades, budget_sek)
        if not s:
            continue
        results.append({
            "label":    label,
            "ticker":   ticker,
            "direction":direction,
            "leverage": leverage,
            "strategy": strategy,
            "n_trades": s["n_trades"],
            "wr":       s["win_rate"],
            "pf":       s["profit_factor"],
            "return":   s["return_pct"],
            "max_dd":   s["max_dd_pct"],
            "avg_days": s["avg_days"],
            "slippage": s["avg_slippage_pct"],
        })
    results.sort(key=lambda r: r["return"], reverse=True)
    return results


def _print_table(results: list[dict], years: float) -> None:
    SEP = "─"
    print(f"\n{'='*112}")
    print(f"  AVANZA CERTIFICATES SWEEP  —  {years:.0f}-year backtest  "
          f"(2,000 SEK budget, 20-day MA,  DAILY REBALANCING)")
    print(f"{'='*112}")
    hdr = (f"  {'Label':<18} {'Ticker':<8} {'Dir':<6} {'Lev':>5} "
           f"{'Strategy':<10} {'Trd':>4} {'WR%':>5} {'PF':>5} "
           f"{'Ret%':>7} {'MaxDD':>6} {'AvgSlip':>8} {'AvgDays':>7}")
    print(hdr)
    print(f"  {SEP*108}")

    prev_ticker = None
    for r in results:
        if r["ticker"] != prev_ticker and prev_ticker is not None:
            print(f"  {SEP*108}")
        prev_ticker = r["ticker"]

        ret_str = f"{r['return']:+.0f}%"
        pf_str  = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "∞"
        slip_str = f"{r['slippage']:+.1f}%"
        mark = "✓" if r["return"] > 0 else "✗"
        print(f"  {r['label']:<18} {r['ticker']:<8} {r['direction']:<6} "
              f"{r['leverage']:>4.0f}x {r['strategy']:<10} "
              f"{r['n_trades']:>4} {r['wr']:>5.1f} {pf_str:>5} "
              f"{ret_str:>7} {r['max_dd']:>5.1f}% {slip_str:>8} "
              f"{r['avg_days']:>6.1f}d  {mark}")

    print(f"  {SEP*108}")

    top = [r for r in results if r["return"] > 0 and r["pf"] >= 1.2]
    if top:
        print(f"\n  TOP PICKS (return>0, PF>=1.2):\n")
        for i, r in enumerate(top[:8], 1):
            pf_str = f"{r['pf']:.2f}" if r["pf"] != float("inf") else "∞"
            print(f"  {i:>2}. {r['label']:<18} {r['direction']:<6} {r['leverage']:>4.0f}x  "
                  f"{r['strategy']:<10}  WR={r['wr']:.0f}%  PF={pf_str}  "
                  f"Ret={r['return']:+.0f}%  DD={r['max_dd']:.0f}%  "
                  f"BetaSlip={r['slippage']:+.1f}%/trade")
    else:
        print("\n  No candidates passed filters (Ret>0, PF>=1.2).")

    print(f"""
  ── Certificate mechanics reminder ──────────────────────────────────────────
  Beta slippage (AvgSlip): difference between naive leveraged return and actual
  compounded return. Negative = compounding HURTS you vs simple leverage.
  At 20x: a 5% adverse single day wipes the ENTIRE position.
  At 10x: a 10% adverse single day wipes the ENTIRE position.
  Certificates are designed for DAY TRADING, not multi-day swing holds.
  ─────────────────────────────────────────────────────────────────────────────
""")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Avanza certificates (DLC) sweep backtest."
    )
    p.add_argument("--years",  type=float, default=5.0)
    p.add_argument("--budget", type=float, default=2000.0)
    args = p.parse_args()

    print(f"\n  Downloading data for {len(set(c[1] for c in CANDIDATES))} "
          f"underlyings, running {len(CANDIDATES)} combinations over "
          f"{args.years:.0f} years...\n")

    for ticker in dict.fromkeys(c[1] for c in CANDIDATES):
        data = _download(ticker, args.years)
        if data[0]:
            closes, dates = data
            print(f"  {ticker:<10} {len(closes)} bars  "
                  f"({dates[0]} -> {dates[-1]})")
        else:
            print(f"  {ticker:<10} NO DATA")

    results = run_sweep(years=args.years, budget_sek=args.budget)
    _print_table(results, args.years)


if __name__ == "__main__":
    main()
