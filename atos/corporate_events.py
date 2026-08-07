"""
atos/corporate_events.py
------------------------
Fetches upcoming ex-dividend and earnings dates for a list of tickers via
yfinance and flags positions that should be exited before the event.

WHY:
  Ex-dividend: stock price is marked down by ~the dividend amount on ex-date.
  For a momentum strategy holding growth stocks (e.g. AAPL, MSFT) the dividend
  yield is small, but the principle still applies — we avoid holding through the
  mechanical price drop and re-enter after.

  Earnings: binary outcome. A strong momentum stock can gap -15% on a miss.
  By default we exit 1 day before earnings and re-evaluate after.

USAGE (in atos_runner.py):
  from atos.corporate_events import get_exit_flags

  flags = get_exit_flags(held_tickers)   # {ticker: reason_str}
  for ticker, reason in flags.items():
      # execute sell, log reason

USAGE (in compute_targets / universe filter):
  from atos.corporate_events import tickers_to_avoid

  avoid = tickers_to_avoid(candidate_tickers)  # set of tickers to exclude this week
  targets = [t for t in raw_targets if t not in avoid]

Results are cached per calendar day so the engine can call these functions
multiple times without re-hitting Yahoo Finance each time.
"""
import datetime
import logging
from functools import lru_cache

import yfinance as yf

log = logging.getLogger(__name__)

# ── Configurable thresholds ────────────────────────────────────────────────
EXDIV_WARN_DAYS    = 3   # Sell N calendar days before ex-dividend date
EARNINGS_WARN_DAYS = 2   # Sell N calendar days before earnings date
AVOID_EARNINGS     = True  # Set False to hold through earnings (ride momentum)
# ──────────────────────────────────────────────────────────────────────────


def _today() -> datetime.date:
    return datetime.date.today()


def _fetch_one(ticker: str) -> dict:
    """Fetch calendar for a single ticker. Returns {} on any error."""
    try:
        cal = yf.Ticker(ticker).calendar
        if not cal:
            return {}
        result = {}
        exdiv = cal.get("Ex-Dividend Date")
        if exdiv:
            result["ex_div"] = exdiv if isinstance(exdiv, datetime.date) else exdiv.date()
        earnings_raw = cal.get("Earnings Date")
        if earnings_raw:
            # yfinance returns a list [start, end] or a single date
            if isinstance(earnings_raw, (list, tuple)):
                result["earnings"] = min(
                    (d if isinstance(d, datetime.date) else d.date())
                    for d in earnings_raw
                )
            else:
                result["earnings"] = (
                    earnings_raw if isinstance(earnings_raw, datetime.date)
                    else earnings_raw.date()
                )
        return result
    except Exception as exc:
        log.debug("corporate_events: %s fetch failed: %s", ticker, exc)
        return {}


# Cache keyed on (ticker, today) so it refreshes each calendar day
@lru_cache(maxsize=512)
def _cached_fetch(ticker: str, today: datetime.date) -> tuple:
    """Returns (ex_div_date_or_None, earnings_date_or_None)."""
    data = _fetch_one(ticker)
    return data.get("ex_div"), data.get("earnings")


def fetch_events(tickers: list, today: datetime.date | None = None) -> dict:
    """Return raw event data for each ticker.

    Returns: {ticker: {"ex_div": date_or_None, "earnings": date_or_None}}
    """
    if today is None:
        today = _today()
    result = {}
    for t in tickers:
        ex_div, earnings = _cached_fetch(t, today)
        result[t] = {"ex_div": ex_div, "earnings": earnings}
    return result


def get_exit_flags(
    held_tickers: list,
    today: datetime.date | None = None,
    exdiv_days: int = EXDIV_WARN_DAYS,
    earnings_days: int = EARNINGS_WARN_DAYS,
    avoid_earnings: bool = AVOID_EARNINGS,
) -> dict:
    """Check current holdings for upcoming corporate events.

    Returns: {ticker: reason_str} for positions that should be sold NOW.
    Empty dict = no action needed.

    Caller should sell these positions before the event, then let the weekly
    rebalance re-buy them afterward if they still qualify.
    """
    if today is None:
        today = _today()
    events = fetch_events(held_tickers, today)
    flags = {}

    for ticker, ev in events.items():
        ex_div = ev.get("ex_div")
        earnings = ev.get("earnings")

        if ex_div:
            days_to_exdiv = (ex_div - today).days
            if 0 <= days_to_exdiv <= exdiv_days:
                flags[ticker] = (
                    f"EXIT before ex-dividend {ex_div} "
                    f"(in {days_to_exdiv}d — avoid mechanical price drop)"
                )

        if avoid_earnings and earnings and ticker not in flags:
            days_to_earnings = (earnings - today).days
            if 0 <= days_to_earnings <= earnings_days:
                flags[ticker] = (
                    f"EXIT before earnings {earnings} "
                    f"(in {days_to_earnings}d — avoid binary gap risk)"
                )

    return flags


def tickers_to_avoid(
    candidate_tickers: list,
    today: datetime.date | None = None,
    exdiv_days: int = EXDIV_WARN_DAYS,
    earnings_days: int = EARNINGS_WARN_DAYS,
    avoid_earnings: bool = AVOID_EARNINGS,
) -> set:
    """Tickers we should NOT buy this week (event is too close).

    Use this to filter the rebalance target list so the engine doesn't open a
    new position a few days before a price-moving event.
    """
    if today is None:
        today = _today()
    events = fetch_events(candidate_tickers, today)
    avoid = set()

    for ticker, ev in events.items():
        ex_div = ev.get("ex_div")
        earnings = ev.get("earnings")
        if ex_div:
            days = (ex_div - today).days
            if 0 <= days <= exdiv_days:
                avoid.add(ticker)
                log.info("  [events] skip %s — ex-div %s in %dd", ticker, ex_div, days)
        if avoid_earnings and earnings and ticker not in avoid:
            days = (earnings - today).days
            if 0 <= days <= earnings_days:
                avoid.add(ticker)
                log.info("  [events] skip %s — earnings %s in %dd", ticker, earnings, days)

    return avoid


def print_upcoming_events(tickers: list, lookahead_days: int = 30) -> None:
    """Print all events within the next N days. Useful for manual review."""
    today = _today()
    events = fetch_events(tickers, today)
    rows = []
    for ticker, ev in events.items():
        for kind, date in [("Ex-Div", ev.get("ex_div")), ("Earnings", ev.get("earnings"))]:
            if date is None:
                continue
            days = (date - today).days
            if 0 <= days <= lookahead_days:
                rows.append((days, ticker, kind, date))
    rows.sort()
    if not rows:
        print(f"No events in next {lookahead_days} days.")
        return
    print(f"\nUpcoming corporate events (next {lookahead_days} days):")
    print(f"  {'Days':>4}  {'Ticker':<8}  {'Type':<8}  Date")
    print("  " + "-" * 36)
    for days, ticker, kind, date in rows:
        flag = " <-- EXIT" if days <= EXDIV_WARN_DAYS else ""
        print(f"  {days:>4}  {ticker:<8}  {kind:<8}  {date}{flag}")


if __name__ == "__main__":
    # Quick check: run against the full US universe
    from atos.universe import US_TICKERS
    print_upcoming_events(US_TICKERS, lookahead_days=14)
    print("\nPositions to exit (if all were held):")
    flags = get_exit_flags(US_TICKERS)
    if flags:
        for t, reason in flags.items():
            print(f"  {t}: {reason}")
    else:
        print("  None")
