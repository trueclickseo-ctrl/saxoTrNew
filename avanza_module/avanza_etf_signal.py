"""
avanza_etf_signal.py
--------------------
Momentum signal for the Avanza ETF strategy.

Downloads 6 months of daily closes from Yahoo Finance for each ETF
in the universe, computes 3-month and 6-month returns, and ranks by
a weighted momentum score:

    score = w_3m * return_3m + w_6m * return_6m   (default 50/50)

Returns the ranked list with BUY / HOLD / EXIT recommendations based
on current positions and top_n / exit_rank thresholds from config.

Yahoo Finance is used for HISTORICAL data only (backtesting + live ranking).
Live execution prices always come from the Avanza API.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


def _yf_download(yahoo_ticker: str, days: int = 190) -> list[float]:
    """Return list of adjusted daily closes, oldest first. Empty on failure."""
    try:
        import yfinance as yf
        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        df    = yf.download(yahoo_ticker, start=start.strftime("%Y-%m-%d"),
                            end=end.strftime("%Y-%m-%d"),
                            auto_adjust=True, progress=False)
        if df.empty:
            return []
        closes = df["Close"].dropna()
        if hasattr(closes, "squeeze"):
            closes = closes.squeeze()
        return [float(x) for x in closes.tolist()]
    except Exception as exc:
        print(f"  [signal] yfinance {yahoo_ticker}: {exc}", file=sys.stderr)
        return []


def _pct_return(closes: list[float], days: int) -> float | None:
    """Return the percentage return over the last `days` trading days.
    Returns None if not enough data.
    """
    if len(closes) < days + 1:
        return None
    price_now  = closes[-1]
    price_then = closes[-(days + 1)]
    if price_then <= 0:
        return None
    return (price_now - price_then) / price_then * 100


def compute_scores(universe: list[dict],
                   w_3m: float = 0.5,
                   w_6m: float = 0.5) -> list[dict]:
    """Download data and compute momentum scores for all universe ETFs.

    Returns list of dicts sorted best-first:
        {ticker, yahoo, name, region, return_3m, return_6m, score,
         current_price, last_updated}

    ETFs with insufficient data get score = None and rank last.
    """
    DAYS_3M = 63   # ~3 calendar months of trading days
    DAYS_6M = 126  # ~6 calendar months

    results = []
    for etf in universe:
        ticker = etf["ticker"]
        yahoo  = etf.get("yahoo", ticker)
        closes = _yf_download(yahoo, days=200)

        r3 = _pct_return(closes, DAYS_3M)
        r6 = _pct_return(closes, DAYS_6M)

        if r3 is not None and r6 is not None:
            score = w_3m * r3 + w_6m * r6
            cur_price = closes[-1] if closes else None
        else:
            score = None
            cur_price = closes[-1] if closes else None

        results.append({
            "ticker":        ticker,
            "yahoo":         yahoo,
            "name":          etf.get("name", ticker),
            "region":        etf.get("region", ""),
            "return_3m":     round(r3, 2) if r3 is not None else None,
            "return_6m":     round(r6, 2) if r6 is not None else None,
            "score":         round(score, 3) if score is not None else None,
            "current_price": round(cur_price, 4) if cur_price else None,
            "last_updated":  datetime.now().isoformat(),
        })

    # Rank: scored first (descending), unscored last
    scored   = sorted([r for r in results if r["score"] is not None],
                      key=lambda x: x["score"], reverse=True)
    unscored = [r for r in results if r["score"] is None]
    ranked   = scored + unscored

    for i, r in enumerate(ranked):
        r["rank"] = i + 1

    return ranked


def print_scores(ranked: list[dict], top_n: int = 3, exit_rank: int = 5,
                 held_tickers: set | None = None) -> None:
    """Print the momentum ranking table."""
    held = held_tickers or set()
    print(f"\n  {'Rank':>4}  {'Ticker':8s}  {'Name':35s}  {'3m%':>7}  {'6m%':>7}  {'Score':>8}  Action")
    print("  " + "-" * 85)
    for r in ranked:
        ticker  = r["ticker"]
        rank    = r["rank"]
        r3      = f"{r['return_3m']:+.1f}%" if r["return_3m"] is not None else "  n/a "
        r6      = f"{r['return_6m']:+.1f}%" if r["return_6m"] is not None else "  n/a "
        sc      = f"{r['score']:+.2f}" if r["score"] is not None else "  n/a"

        if rank <= top_n and ticker not in held:
            action = "BUY"
        elif rank <= top_n and ticker in held:
            action = "HOLD"
        elif ticker in held and rank > exit_rank:
            action = "SELL (out of top-5)"
        elif ticker in held:
            action = "HOLD (monitor)"
        else:
            action = ""

        marker = ">>>" if rank <= top_n else "   "
        print(f"  {marker}{rank:>3}  {ticker:8s}  {r['name'][:35]:35s}  "
              f"{r3:>7}  {r6:>7}  {sc:>8}  {action}")
    print()
