"""
ibkr_price_service.py
-----------------------
IBKR equivalent of price_service.py -- fetches live mid-prices for a list
of instruments, used the same way: checking current price against a stop
level, sizing an order right before submission, etc. (Historical bars for
signal generation still come from yfinance via live_data.py, unchanged --
this module only covers the "live Saxo mid-price" role price_service.py
played, not backtesting data.)

WHAT'S DIFFERENT FROM price_service.py
-----------------------------------------
No `token` parameter -- there's no per-request auth token with IBKR the
way there is with Saxo's bearer token, so fetch_prices() drops it. Any
call site passing `token=...` explicitly needs that argument removed;
callers that used the default (None) are unaffected.

THREADING NOTE
---------------
ib_async/ib_insync wraps a single asyncio event loop tied to whichever
thread called ibkr_client.connect() -- every ib.* call (reqMktData, sleep,
cancelMktData) has to happen on that same thread. fetch_prices() therefore
subscribes to every instrument first, waits once, then reads and
unsubscribes -- all on the calling thread -- rather than fanning out across
a thread pool (which ib_async does not support and will misbehave under).
One shared 1s wait covers every symbol, so this is not slower than the
per-symbol-thread approach it replaces.
"""

from __future__ import annotations

import ibkr_client


def _valid(x: float | None) -> bool:
    """IBKR uses -1 (and sometimes NaN) as a "field not available" sentinel
    on Ticker, not just missing/None -- a naive truthy/NaN check reads -1 as
    a real price. Reject anything <= 0 too."""
    return x is not None and x == x and x > 0


def fetch_prices(instruments: list[dict]) -> tuple[dict[str, float], str]:
    """
    Fetch live mid-prices for a list of instruments.

    instruments: list of dicts, each needing at minimum
                 {"symbol": <key to return under>, "uic": <conId>}
                 (matches the shape price_service.fetch_prices() expects,
                 minus Saxo's asset_type requirement -- IBKR resolves that
                 from the cached contract, not from the caller).

    Returns (prices, status) where prices maps symbol -> mid price for
    every instrument that resolved successfully, and status is "ok" or
    "partial" (mirrors price_service's return shape so callers checking
    the status string don't need to change).
    """
    ib = ibkr_client._client()

    subs: list[tuple[str, object]] = []   # (symbol, contract)
    for inst in instruments:
        try:
            contract = ibkr_client._resolve_by_conid(inst["uic"])
        except Exception:
            continue
        ib.reqMktData(contract, "", False, False)
        subs.append((inst["symbol"], contract))

    if subs:
        # bid/ask/last often arrive within ~1s; `close` (the fallback used
        # below when bid/ask/last are unavailable) took ~3-4s on a fresh
        # subscription in testing (2026-08-21: reliably present at 3s,
        # absent at 2.5s for a single-symbol AAPL subscription) -- 3.5s
        # is a safety margin over that, still a compromise not a guarantee.
        ib.sleep(3.5)

    prices: dict[str, float] = {}
    for symbol, contract in subs:
        ticker = ib.ticker(contract)
        price = None
        if ticker:
            bid, ask, last, close = ticker.bid, ticker.ask, ticker.last, ticker.close
            if _valid(bid) and _valid(ask):
                price = (bid + ask) / 2
            elif _valid(last):
                price = last
            elif _valid(close):
                # Top-of-book (bid/ask/last) is frequently unavailable even
                # on delayed data without extra entitlements -- previous
                # close is the last resort, same "some price beats none"
                # reasoning price_service.py applies with LastTraded.
                price = close
        if price is not None:
            prices[symbol] = price
        ib.cancelMktData(contract)

    status = "ok" if len(prices) == len(instruments) else "partial"
    return prices, status
