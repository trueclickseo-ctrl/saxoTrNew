"""
data_loader.py
---------------
Pulls historical daily price data for backtesting.

NOTE: Saxo's Simulation (SIM/paper) environment does NOT provide historical
market data for stocks — it's only for testing order execution. So we use
Yahoo Finance here purely to build and validate the STRATEGY LOGIC. Once
we move to paper trading against Saxo's SIM API, prices there will come
live from Saxo instead — this file's job ends at the backtesting stage.
"""

import pandas as pd
import yfinance as yf
import os
import time

CACHE_DIR = "data"

# Yahoo Finance rate-limits bulk requests. When several tickers are pulled back
# to back, a few will randomly fail with a "possibly delisted" message even
# though the ticker is fine — it's really a throttling response. Retrying
# after a pause (with increasing backoff) clears almost all of these.
MAX_RETRIES = 6
RETRY_DELAY_SECONDS = 15
BASE_DELAY_BETWEEN_TICKERS = 3


def load_prices(ticker: str, start: str, end: str | None = None) -> pd.DataFrame:
    """
    Returns a DataFrame with columns: Open, High, Low, Close, Volume
    indexed by date, for one ticker. Caches to disk so repeat backtests
    are fast and don't hammer the data source.
    """
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{ticker.replace('.', '_')}.csv")

    if os.path.exists(cache_path):
        df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
        return df

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
            if not df.empty:
                break
            last_error = "empty response"
        except Exception as e:
            last_error = str(e)

        if attempt < MAX_RETRIES:
            delay = RETRY_DELAY_SECONDS * attempt   # backs off: 15s, 30s, 45s...
            print(f"    {ticker}: attempt {attempt} failed ({last_error}), retrying in {delay}s...")
            time.sleep(delay)
    else:
        raise ValueError(f"No data returned for {ticker} after {MAX_RETRIES} attempts ({last_error}).")

    if df.empty:
        raise ValueError(f"No data returned for {ticker} after {MAX_RETRIES} attempts ({last_error}).")

    # yfinance sometimes returns MultiIndex columns for a single ticker — flatten if so
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.to_csv(cache_path)
    # Pause between tickers so we don't trigger the rate limit in the first place
    time.sleep(BASE_DELAY_BETWEEN_TICKERS)
    return df


def load_universe(tickers: list[str], start: str, end: str | None = None) -> dict[str, pd.DataFrame]:
    """Loads price data for a whole list of tickers, skipping any that fail."""
    data = {}
    failed = []
    for t in tickers:
        try:
            data[t] = load_prices(t, start, end)
            print(f"  loaded {t}: {len(data[t])} rows")
        except Exception as e:
            print(f"  SKIPPED {t}: {e}")
            failed.append(t)

    if failed:
        print(f"\n  {len(failed)} ticker(s) failed after retries: {failed}")
        print("  These are excluded from this run. Re-running main.py will use the cache")
        print("  for tickers that succeeded and only retry the failed ones.")

    return data
