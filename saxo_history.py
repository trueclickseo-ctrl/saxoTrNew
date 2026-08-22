"""
saxo_history.py  —  Live daily OHLCV history from Saxo, for stocks
--------------------------------------------------------------------
Source:  Saxo SIM API  (/chart/v3/charts)
         Confirmed live 2026-08-22 that Saxo's SIM environment DOES serve
         real historical daily bars for US/other stocks (a prior comment in
         data_loader.py claiming otherwise was stale/never re-verified — see
         [[saxo_api_verification]]). Per explicit user direction: any LIVE
         trading decision (position sizing, indicator calculation, order
         placement) must use Saxo, never Yahoo -- Yahoo/yfinance stays for
         historical/backtest code only (data_loader.py, backtest_*.py).

         The chart endpoint has its OWN rate-limit bucket, separate from
         the general one -- confirmed live: 120 requests/minute
         (X-RateLimit-ChartMinute-Limit), reset every ~60s
         (X-RateLimit-ChartMinute-Reset). An unthrottled concurrent fetch
         of a 385-ticker universe blew through this in ~20s and then
         failed the remaining ~185 tickers with 429s -- including large،
         obviously-covered names (AMZN, JPM, WMT...), not a data-coverage
         gap. _RateLimiter below paces requests to stay under the limit;
         a full 385-ticker universe now takes a few minutes instead of
         failing halfway, which is fine for a once-daily cycle.

Usage:
    from saxo_history import fetch_daily_bars
    bars = fetch_daily_bars(["AAPL", "MSFT", ...], count=300)
    # bars = {"AAPL": DataFrame[Open,High,Low,Close,Volume], ...}
    # Same shape as yf.download(...)[ticker] -- drop-in replacement.
"""

import threading
import time
from collections import deque

import pandas as pd
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import saxo_client
from instrument_map import load_instrument_map

SIM_BASE = saxo_client.SIM_BASE_URL

# Saxo's real chart-endpoint limit is 120/min -- pace to a safer 100/min so
# a few concurrent workers plus any other process sharing this app's quota
# (e.g. a scheduled forex/futures run using the same chart endpoint at the
# same moment) doesn't still tip it over.
CHART_REQUESTS_PER_MINUTE = 100


class _RateLimiter:
    """Sliding-window limiter: blocks until fewer than `per_minute` calls
    have happened in the trailing 60s. Shared across worker threads."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._calls = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > 60:
                    self._calls.popleft()
                if len(self._calls) < self.per_minute:
                    self._calls.append(now)
                    return
                sleep_for = 60 - (now - self._calls[0]) + 0.05
            time.sleep(max(sleep_for, 0.05))


def _fetch_one(limiter: _RateLimiter, token: str, uic: int,
                asset_type: str, count: int) -> pd.DataFrame | None:
    for attempt in range(2):
        limiter.wait()
        try:
            r = requests.get(
                f"{SIM_BASE}/chart/v3/charts",
                headers={"Authorization": f"Bearer {token}"},
                params={"Uic": uic, "AssetType": asset_type, "Horizon": 1440, "Count": count},
                timeout=15,
            )
            if r.status_code == 429:
                reset = r.headers.get("X-RateLimit-ChartMinute-Reset")
                try:
                    time.sleep(max(1.0, float(reset)) + 0.5)
                except (TypeError, ValueError):
                    time.sleep(5.0)
                continue
            if r.status_code != 200:
                return None
            rows = r.json().get("Data", [])
            if not rows:
                return None
            df = pd.DataFrame(rows)
            df["Time"] = pd.to_datetime(df["Time"])
            df = df.set_index("Time").sort_index()
            return df[["Open", "High", "Low", "Close", "Volume"]]
        except Exception:
            return None
    return None


def fetch_daily_bars(tickers: list[str], count: int = 300,
                      asset_type: str = "Stock", min_bars: int = 50,
                      max_workers: int = 6) -> dict[str, pd.DataFrame]:
    """
    Fetch `count` daily OHLCV bars for each ticker from Saxo, rate-limited
    to CHART_REQUESTS_PER_MINUTE.

    Tickers missing from data/instrument_map.csv (no UIC) are skipped, same
    as they'd be unsizeable/untradeable anyway. Any ticker whose fetch
    fails (network error, or a 429 that didn't clear after its own
    in-request retry) or returns fewer than `min_bars` rows is dropped
    from the result (matches atos_runner.py's own download_universe()
    quality filter) -- callers should expect a possibly-smaller dict than
    `tickers`, not a KeyError.
    """
    imap = load_instrument_map()
    token = saxo_client.get_token()
    limiter = _RateLimiter(CHART_REQUESTS_PER_MINUTE)

    jobs = {t: imap[t]["uic"] for t in tickers if t in imap}

    result: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(_fetch_one, limiter, token, uic, asset_type, count): t
            for t, uic in jobs.items()
        }
        for fut in as_completed(futures):
            t  = futures[fut]
            df = fut.result()
            if df is not None and len(df) >= min_bars:
                result[t] = df

    return result
