"""
compare_markets.py
-------------------
Breaks down results/trade_log.csv by market (Sweden/OMX30, Copenhagen,
US/Nasdaq, Germany/DAX) so you can see which market this strategy has
actually performed best in — not a guess, the real numbers from your
own backtest.

Run this AFTER main.py has produced results/trade_log.csv with the full
universe (OMX30 + Copenhagen + Nasdaq + Germany).

    python compare_markets.py

READ THE CAVEATS PRINTED AT THE BOTTOM — a market "winning" this
comparison can easily be an artifact of which few stocks you picked and
what happened to do well over this specific historical window, not proof
that market is inherently better for this strategy.
"""

import pandas as pd
import config


def market_for_ticker(ticker: str) -> str:
    if ticker in config.OMX30_TICKERS:
        return "Sweden (OMX30)"
    if ticker in config.COPENHAGEN_TICKERS:
        return "Denmark (Copenhagen)"
    if ticker in config.NASDAQ100_TICKERS:
        return "USA (Nasdaq)"
    if ticker in config.GERMANY_TICKERS:
        return "Germany (DAX)"
    if ticker in config.UK_TICKERS:
        return "UK (FTSE)"
    if ticker in config.FRANCE_TICKERS:
        return "France (CAC40)"
    if ticker in config.NETHERLANDS_TICKERS:
        return "Netherlands (AEX)"
    if ticker in config.SWITZERLAND_TICKERS:
        return "Switzerland (SMI)"
    if ticker in config.CANADA_TICKERS:
        return "Canada (TSX)"
    if ticker in config.JAPAN_TICKERS:
        return "Japan (Nikkei)"
    return "Unknown"


def main():
    df = pd.read_csv("results/trade_log.csv")
    df["market"] = df["ticker"].apply(market_for_ticker)

    summary = df.groupby("market").agg(
        num_trades=("pnl", "count"),
        total_pnl=("pnl", "sum"),
        avg_pnl_per_trade=("pnl", "mean"),
        win_rate_pct=("pnl", lambda x: round((x > 0).mean() * 100, 1)),
    ).sort_values("total_pnl", ascending=False)

    summary["total_pnl"] = summary["total_pnl"].round(2)
    summary["avg_pnl_per_trade"] = summary["avg_pnl_per_trade"].round(2)

    print("=" * 70)
    print("PERFORMANCE BY MARKET")
    print("=" * 70)
    print(summary.to_string())
    print("=" * 70)

    # Concentration check per market: how much of each market's profit
    # comes from just its single best trade? High = fragile result.
    print("\nConcentration check (biggest single trade as % of that market's total profit):")
    for market in summary.index:
        sub = df[df["market"] == market]
        if sub["pnl"].sum() > 0:
            biggest = sub["pnl"].max()
            pct = (biggest / sub["pnl"].sum()) * 100
            print(f"  {market}: best single trade = {biggest:.2f} "
                  f"({pct:.1f}% of that market's total profit)")

    print("\n" + "=" * 70)
    print("CAVEATS — read before concluding anything")
    print("=" * 70)
    print("""
1. Different number of stocks per market means totals aren't directly
   comparable — compare avg_pnl_per_trade and win_rate_pct instead of
   total_pnl for a fairer per-trade read.
2. The US/Nasdaq list is today's biggest tech winners, selected with
   hindsight. A strong US result here partly reflects that those specific
   companies happened to have extraordinary runs in this exact historical
   window — not proof the US market is inherently more profitable to
   trade going forward.
3. Small sample sizes per market (tens of trades, not hundreds) mean a
   couple of lucky/unlucky trades can swing win_rate_pct and avg_pnl
   substantially. Check the concentration numbers above before trusting
   any market's ranking.
4. This whole comparison is for the CURRENT trend-following strategy only
   — a different market might rank differently under a different strategy
   entirely. This tells you "where has THIS strategy worked," not "which
   market is universally better."
5. You're now comparing 10 markets at once. Testing many markets and
   picking whichever looks best is a real statistical trap (multiple
   comparisons) — with this many groups, one will look great by chance
   alone even with zero real edge. Treat the TOP market here as a
   hypothesis to test further, not a proven winner.
""")


if __name__ == "__main__":
    main()
