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
    # Added 2026-08-21 — these three aren't just "live fetch failed today,"
    # yfinance's f"{ccy}SEK=X" ticker returns "possibly delisted, no price
    # data" for TRY/MXN/CNH specifically (unlike every other currency here,
    # which fetches live fine). With no fallback, every pair quoted in one of
    # these (AUDTRY, CADTRY, USDMXN, EURCNH, GBPCNH, ...) was permanently
    # unsizable — silently dropped from the universe on every single scan,
    # not a transient outage. Approximate pegs, same caveat as the rest of
    # this table (drift over time, log loudly when used, don't trust for
    # real sizing) — verify against a live rate before trading real capital
    # in these currencies.
    "TRY": 0.21,   # Turkish lira
    "MXN": 0.55,   # Mexican peso
    "CNH": 1.41,   # offshore Chinese yuan (renminbi)
    # Added 2026-08-22 — user hit this live: forex_dashboard.py raised on
    # CZKSEK=X and printed the exact same "possibly delisted" symptom as the
    # three above for ILS. Checked every quote currency actually used in
    # forex/universe.py's 117-pair universe (23 total) against a live fetch
    # that day: ILS fails 100% of the time with the same KeyError
    # ('exchangeTimezoneName') as TRY/MXN/CNH — a structurally broken ticker,
    # not a transient outage (rate below triangulated via ILS=X / USDSEK=X
    # since ILSSEK=X itself never returns anything). CZK fetched live fine
    # every time it was tested that same day, but had zero fallback, so ANY
    # transient Yahoo hiccup on it raises RuntimeError with no safety net —
    # added for resilience, same as the rest of this table. The other
    # currencies below (AED/AUD/HKD/HUF/NOK/NZD/PLN/RON/SGD/THB/ZAR) fetch
    # live fine but were previously undocumented/unprotected the same way —
    # filled in from live rates the same day so a future flake on any of
    # them degrades to a loud warning instead of a silent 1.0-rate P&L
    # distortion (forex_dashboard.py's _eur_per_unit swallows the exception
    # and falls back to rate=1.0 unconditionally when this table has no
    # entry — wildly wrong for anything but literal EUR).
    "CZK": 0.44,   # Czech koruna
    "ILS": 3.17,   # Israeli shekel (triangulated: USDSEK / USDILS)
    "AED": 2.57,   # UAE dirham
    "AUD": 6.78,   # Australian dollar
    "HKD": 1.21,   # Hong Kong dollar
    "HUF": 0.030,  # Hungarian forint
    "NOK": 1.02,   # Norwegian krone
    "NZD": 5.65,   # New Zealand dollar
    "PLN": 2.57,   # Polish zloty
    "RON": 2.10,   # Romanian leu
    "SGD": 7.45,   # Singapore dollar
    "THB": 0.28,   # Thai baht
    "ZAR": 0.59,   # South African rand
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
