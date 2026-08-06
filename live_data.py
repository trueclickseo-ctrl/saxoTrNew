"""
live_data.py
------------
Fetches CURRENT price history for the live engine. Deliberately separate
from data_loader.py, which caches to disk forever — correct for
reproducible backtests, wrong for a live signal that needs today's actual
close. This always fetches fresh (no cache).

Only pulls enough history (LOOKBACK_DAYS) to compute the slow moving
average and ATR, not the full multi-year backtest range — keeps daily runs
fast.
"""

import pandas as pd
import yfinance as yf

LOOKBACK_DAYS = 150  # comfortably more than SLOW_MA (50) + ATR_PERIOD (14) need


def get_latest_universe_data(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Fetches recent daily OHLC data for every ticker. Skips (and logs) any
    that fail rather than aborting the whole run over one bad ticker."""
    data = {}
    for t in tickers:
        try:
            df = yf.download(t, period=f"{LOOKBACK_DAYS}d", progress=False, auto_adjust=True)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if df.empty:
                print(f"  [WARN] No data returned for {t}, skipping this cycle")
                continue
            data[t] = df
        except Exception as e:
            print(f"  [WARN] Failed to fetch {t}: {e}")
    return data
