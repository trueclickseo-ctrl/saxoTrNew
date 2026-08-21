"""
ibkr_history.py
-----------------
Historical OHLC bars from IBKR, via ib.reqHistoricalData(). New capability --
Saxo's forex/runner.py pulls every strategy's bars directly from Saxo's own
/chart/v3/charts endpoint (not yfinance/live_data.py, unlike the US-stock and
ETF modules), so there was no existing "historical bars" function in
ibkr_client.py to reuse for that migration. Kept in its own module rather
than folded into ibkr_client.py, matching the existing split between
ibkr_client.py (account/orders), ibkr_order.py (stop/TP brackets), and
ibkr_price_service.py (live quotes).

Returns plain DataFrames with Open/High/Low/Close columns (no broker-specific
fields) so callers written against Saxo's bar-fetch functions need only a
call-site swap, not a data-shape change.
"""

from __future__ import annotations

import pandas as pd

import ibkr_client


def get_bars(
    uic: int,
    bar_size: str = "1 day",
    duration: str = "300 D",
    what_to_show: str = "MIDPOINT",
    use_rth: bool = False,
) -> pd.DataFrame | None:
    """
    Fetch historical bars for a resolved instrument (conId from
    find_instrument()). Returns a DataFrame with Open/High/Low/Close columns
    (oldest first), or None on failure/empty result -- never raises, same
    "never raise, return None" contract as price_service._saxo_mid() and the
    Saxo chart-fetch helpers this replaces.

    bar_size: IBKR barSizeSetting, e.g. "1 day", "1 hour", "1 min".
    duration: IBKR durationStr, e.g. "300 D", "2 D", "6 M". Must be long
              enough to cover bar_size * however many bars the caller needs --
              IBKR derives bar count from (duration / bar_size), there's no
              direct "give me N bars" parameter.
    what_to_show: "MIDPOINT" for FX/CFD-style instruments (no real last-trade
              tape); "TRADES" for exchange-listed stocks/futures with an
              actual tape.
    """
    try:
        contract = ibkr_client._resolve_by_conid(uic)
    except Exception:
        return None

    ib = ibkr_client._client()
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            # formatDate=2 -> UTC-aware datetimes (both daily and intraday
            # bars), not exchange-local -- unambiguous for callers that need
            # to bucket by UTC hour (e.g. session-based FX strategies).
            formatDate=2,
        )
    except Exception:
        return None

    if not bars:
        return None

    rows = [{"Date": b.date, "Open": b.open, "High": b.high,
             "Low": b.low, "Close": b.close, "Volume": b.volume} for b in bars]
    df = pd.DataFrame(rows)
    return df if len(df) > 0 else None
