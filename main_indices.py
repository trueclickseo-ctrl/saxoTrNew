"""
main_indices.py
----------------
Run this file to backtest the trend-following strategy on the CFD index
universe (DAX, Copenhagen 25, US 500, US Tech 100, US 30).

    python main_indices.py

Separate from main.py (which runs OMX30 as stocks) on purpose — same
strategy signal logic, different cost/sizing model underneath.
"""

import config
from data_loader import load_universe
from backtest_cfd import CFDBacktester
import os


def main():
    tickers = list(config.INDEX_TICKERS.values())
    print(f"Loading historical data for {len(tickers)} indices...")
    universe_data = load_universe(tickers, config.BACKTEST_START, config.BACKTEST_END)

    if not universe_data:
        print("No data loaded — check your internet connection and ticker symbols.")
        return

    # Re-key from Yahoo ticker (e.g. '^GDAXI') back to our label (e.g. 'DAX_DE')
    # so trade logs read as "DAX_DE" instead of a Yahoo symbol.
    ticker_to_label = {v: k for k, v in config.INDEX_TICKERS.items()}
    labeled_data = {ticker_to_label[t]: df for t, df in universe_data.items()}

    print("\nRunning CFD backtest...")
    print("(margin rate and financing rate are placeholder estimates — see config.py)")
    bt = CFDBacktester(labeled_data)
    summary, equity_df, trades_df = bt.run()

    print("\n" + "=" * 50)
    print("CFD INDEX BACKTEST RESULTS")
    print("=" * 50)
    for k, v in summary.items():
        print(f"  {k:24s}: {v}")
    print("=" * 50)

    os.makedirs("results", exist_ok=True)
    equity_df.to_csv("results/index_equity_curve.csv")
    trades_df.to_csv("results/index_trade_log.csv", index=False)
    print("\nSaved: results/index_equity_curve.csv, results/index_trade_log.csv")


if __name__ == "__main__":
    main()
