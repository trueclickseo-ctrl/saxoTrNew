"""
main.py
-------
Run this file to backtest the strategy on the configured universe.

    python main.py

Results print to the terminal and save to results/ as CSVs you can
open in Excel to inspect every single trade the strategy would have made.
"""

import config
from data_loader import load_universe
from backtest import Backtester
import os


def main():
    print(f"Loading historical data for {len(config.ACTIVE_UNIVERSE)} tickers...")
    universe_data = load_universe(config.ACTIVE_UNIVERSE, config.BACKTEST_START, config.BACKTEST_END)

    if not universe_data:
        print("No data loaded — check your internet connection and ticker symbols.")
        return

    print("\nRunning backtest...")
    bt = Backtester(universe_data)
    summary, equity_df, trades_df = bt.run()

    print("\n" + "=" * 50)
    print("BACKTEST RESULTS")
    print("=" * 50)
    for k, v in summary.items():
        print(f"  {k:20s}: {v}")
    print("=" * 50)

    os.makedirs("results", exist_ok=True)
    equity_df.to_csv("results/equity_curve.csv")
    trades_df.to_csv("results/trade_log.csv", index=False)
    print("\nSaved: results/equity_curve.csv, results/trade_log.csv")


if __name__ == "__main__":
    main()
