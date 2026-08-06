"""
fx.py
-----
Position sizing and every cash/cost comparison need to happen in ONE
common currency — SEK, since config.STARTING_CAPITAL and the risk rules
are denominated in SEK. Two different things need converting into SEK:

1. The Saxo SIM account's own currency (EUR) — for equity/cash figures.
2. Each traded instrument's own currency — config.ACTIVE_UNIVERSE now
   spans SEK, USD, EUR, GBP, CHF, CAD, and JPY instruments (OMX30 +
   Copenhagen + Nasdaq-100 + Germany + UK + France + Netherlands +
   Switzerland + Canada + Japan). Comparing a USD or JPY price against a
   SEK cash figure as if they were the same currency is silently wrong,
   not a rounding error.

get_rate_to_sek() handles both cases generically — pass whatever
currency code you have (account currency or instrument currency).
"""

import yfinance as yf

# Rough fallback rates, ONLY used if the live fetch fails. These drift —
# log loudly whenever one is used, don't trust it silently for real sizing.
# Extend this if you add instruments in a currency not listed here.
FALLBACK_RATES_TO_SEK = {
    "SEK": 1.0,
    "EUR": 11.0,
    "USD": 10.3,
    "GBP": 13.0,
    "CHF": 12.0,
    "CAD": 7.5,
    "JPY": 0.068,
    "DKK": 1.5,
}

# Cached per process run so a single cycle doesn't refetch the same rate
# once per ticker — call reset_cache() to force fresh rates on a new run.
_rate_cache: dict[str, float] = {}


def get_rate_to_sek(currency: str) -> float:
    """Returns how many SEK one unit of `currency` is worth."""
    currency = (currency or "").upper()
    if not currency:
        raise ValueError(
            "get_rate_to_sek() called with an empty currency code — check "
            "that data/instrument_map.csv has a currency for this ticker "
            "(rerun lookup_instruments.py if it's missing)."
        )
    if currency == "SEK":
        return 1.0
    if currency in _rate_cache:
        return _rate_cache[currency]

    try:
        fast = yf.Ticker(f"{currency}SEK=X").fast_info
        rate = float(fast["last_price"])
        if rate <= 0:
            raise ValueError(f"Implausible {currency}SEK rate: {rate}")
    except Exception as e:
        fallback = FALLBACK_RATES_TO_SEK.get(currency)
        if fallback is None:
            raise RuntimeError(
                f"No live rate and no fallback for {currency}/SEK. Add one "
                f"to FALLBACK_RATES_TO_SEK in fx.py before trading an "
                f"instrument in this currency."
            ) from e
        print(f"  [WARN] Could not fetch live {currency}/SEK rate ({e}). "
              f"Using fallback {fallback} — VERIFY this is close to reality "
              f"before trusting position sizing this cycle.")
        rate = fallback

    _rate_cache[currency] = rate
    return rate


def get_eur_sek_rate() -> float:
    """Kept for backward compatibility with anything still importing this
    name directly — equivalent to get_rate_to_sek('EUR')."""
    return get_rate_to_sek("EUR")


def reset_cache() -> None:
    """Clears the cached rates. Only matters if this module stays loaded
    across multiple run_cycle() calls in one process (e.g. a scheduler
    that doesn't restart the interpreter each day) — the normal
    once-a-day script invocation doesn't need this."""
    _rate_cache.clear()
