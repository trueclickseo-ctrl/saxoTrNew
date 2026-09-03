"""
saxo_client.py
--------------
Handles all communication with Saxo's OpenAPI Simulation (SIM) environment,
and, since 2026-08-25, the real-money LIVE environment (used only by
housekeeping.py's live-forex reconciliation pass — every other module
still uses SIM exclusively).

SECURITY: the access token is read from an environment variable, never
hardcoded or pasted into this file. See README_SAXO_SETUP.md for how to
set it on your machine.

Every function below takes an `env: str = "sim"` parameter — omitting it
is byte-identical to this file's behavior before LIVE support existed.
`env="live"` points at Saxo's live gateway and a separate account-key /
token (see saxo_auth.py's own env split). NOT YET VERIFIED: LIVE almost
certainly assigns DIFFERENT Uic numbers than SIM for the same instrument
(Saxo's Uics are commonly per-environment) — forex/universe.py's UICs
were only ever confirmed against SIM. Re-verify every CORE pair's live
Uic via find_instrument(..., env="live") before trusting it for a real
order; do not assume SIM's numbers carry over.
"""

import os
import time
import requests
import saxo_auth

SIM_BASE_URL = "https://gateway.saxobank.com/sim/openapi"
LIVE_BASE_URL = "https://gateway.saxobank.com/openapi"   # per Saxo's documented sim->live convention; confirm on your LIVE app's portal page

MAX_RETRIES = 4
RETRY_DELAY_SECONDS = 5


def _base_url(env: str = "sim") -> str:
    # "live_eur" (2026-08-26, forex/runner.py's EUR sub-account) is the
    # same real Saxo LIVE gateway as "live" -- only the AccountKey (see
    # _EXPECTED_CURRENCY/get_account_key below) differs, never the URL.
    if env in ("live", "live_eur"):
        return LIVE_BASE_URL
    if env == "sim":
        return SIM_BASE_URL
    raise ValueError(f"Unknown Saxo env {env!r} -- expected 'sim', 'live', or 'live_eur'.")


def _request_with_retry(method: str, url: str, **kwargs):
    """
    Wraps requests calls with retry logic for transient network/SSL errors,
    which happen occasionally with any API and aren't a sign of anything
    wrong with your setup.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=15, **kwargs)
            return resp
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_error = e
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_SECONDS * attempt
                print(f"    network error ({e.__class__.__name__}), retrying in {delay}s...")
                time.sleep(delay)
    raise last_error


def get_token(env: str = "sim") -> str:
    """
    Prefers the self-refreshing PKCE login (saxo_auth.py) so the bot can run
    unattended. Falls back to a manual 24h token env var if no PKCE login
    has been done yet for that environment — handy for quick one-off tests
    (SAXO_TOKEN for sim, SAXO_LIVE_TOKEN for live).
    """
    try:
        return saxo_auth.get_valid_access_token(env=env)
    except RuntimeError:
        fallback_var = "SAXO_TOKEN" if env == "sim" else "SAXO_LIVE_TOKEN"
        token = os.environ.get(fallback_var)
        if not token:
            login_hint = "python saxo_auth.py" if env == "sim" else "python saxo_auth.py --live"
            raise RuntimeError(
                f"No Saxo {env.upper()} login available. Either run `{login_hint}` "
                "once to log in via PKCE (recommended — self-refreshing), or set "
                f"the {fallback_var} env var with a 24h token for a quick manual "
                "test. See README_SAXO_SETUP.md."
            )
        return token


def _headers(env: str = "sim") -> dict:
    return {"Authorization": f"Bearer {get_token(env=env)}"}


def test_connection(env: str = "sim") -> dict:
    """
    Calls Saxo's 'who am I' endpoint — the simplest possible test that the
    token is valid and the connection works. Returns basic user info.
    """
    resp = _request_with_retry("GET", f"{_base_url(env)}/port/v1/users/me", headers=_headers(env))
    resp.raise_for_status()
    return resp.json()


def get_account_info(env: str = "sim") -> dict:
    """Returns account details, including the AccountKey needed for orders."""
    resp = _request_with_retry("GET", f"{_base_url(env)}/port/v1/accounts/me", headers=_headers(env))
    resp.raise_for_status()
    return resp.json()


def get_positions(env: str = "sim") -> dict:
    """Returns currently open positions on the given account.

    Requests PositionView so that ProfitLossOnTrade and ProfitLossOnTradeBase
    (the net P&L figures Saxo's own UI displays) are included in the response.
    """
    resp = _request_with_retry(
        "GET",
        f"{_base_url(env)}/port/v1/positions/me",
        headers=_headers(env),
        params={"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"},
    )
    resp.raise_for_status()
    return resp.json()


def get_balances(env: str = "sim") -> dict:
    """
    Returns account balance data, including TotalValue (equity) and
    CashAvailableForTrading — this is the source of truth for equity/cash,
    not anything computed locally.
    """
    resp = _request_with_retry("GET", f"{_base_url(env)}/port/v1/balances/me", headers=_headers(env))
    resp.raise_for_status()
    return resp.json()


# Keyed by env — a SIM AccountKey must never leak into a LIVE order and
# vice versa. (get_price_decimals/get_tick_size's caches below are keyed by
# (uic, asset_type) instead, which is already env-safe: if SIM and LIVE
# assign different Uics for the same instrument, as expected, the keys
# simply never collide.)
_account_key_cache: dict = {}

# 2026-08-25: confirmed live that a single Saxo login can control MULTIPLE
# sub-accounts in different currencies (this LIVE login has 3: SEK, EUR,
# USD). Blindly taking accounts/me's Data[0] happened to land on the right
# (SEK) one only because it's listed first -- not guaranteed by Saxo's API
# contract, and a real risk of an order silently going to the wrong
# sub-account if that ordering ever changes. `_EXPECTED_CURRENCY` pins down
# which currency each env's account MUST be; SIM has no entry here because
# it has only ever had one account (unchanged Data[0] behavior for it).
_EXPECTED_CURRENCY = {"live": "SEK", "live_eur": "EUR"}


def get_account_key(env: str = "sim") -> str:
    """AccountKey needed to place orders. Prefer the env var override
    (SAXO_ACCOUNT_KEY for sim, SAXO_LIVE_ACCOUNT_KEY for live); otherwise
    fetch it from the API once and cache it per-environment for this
    process, so orders work out of the box.

    If this env has an expected currency (see _EXPECTED_CURRENCY), the
    matching sub-account is selected explicitly -- if there are multiple
    sub-accounts and NONE match, this raises rather than guessing, since
    guessing wrong here means a real order goes to the wrong account.
    """
    override_var = {"sim": "SAXO_ACCOUNT_KEY", "live": "SAXO_LIVE_ACCOUNT_KEY"}.get(
        env, "SAXO_LIVE_EUR_ACCOUNT_KEY")
    key = os.environ.get(override_var)
    if key:
        return key
    if env in _account_key_cache:
        return _account_key_cache[env]
    info = get_account_info(env=env)
    data = info.get("Data", info)
    accounts = data if isinstance(data, list) and data else ([data] if isinstance(data, dict) else [])
    if not accounts:
        raise RuntimeError(
            f"Could not determine AccountKey from Saxo {env.upper()} "
            f"/port/v1/accounts/me (no accounts returned). Set the "
            f"{override_var} env var explicitly."
        )

    expected_ccy = _EXPECTED_CURRENCY.get(env)
    acct = None
    if expected_ccy:
        acct = next((a for a in accounts if isinstance(a, dict) and a.get("Currency") == expected_ccy), None)
        if acct is None and len(accounts) > 1:
            currencies = [a.get("Currency") for a in accounts if isinstance(a, dict)]
            raise RuntimeError(
                f"Saxo {env.upper()} login has {len(accounts)} sub-accounts "
                f"({currencies}) but none is {expected_ccy}-denominated -- "
                f"refusing to guess which one to trade on. Set {override_var} "
                f"explicitly to the correct AccountKey."
            )
    if acct is None:
        acct = accounts[0]   # unchanged behavior: no currency preference, or exactly one account
    key = acct.get("AccountKey") if isinstance(acct, dict) else None
    if not key:
        raise RuntimeError(
            f"Could not determine AccountKey from Saxo {env.upper()} "
            f"/port/v1/accounts/me. Set the {override_var} env var explicitly."
        )
    _account_key_cache[env] = key
    return key


def find_instrument(symbol: str, asset_type: str = "Stock", env: str = "sim") -> list[dict]:
    """
    Looks up Saxo's internal Uic (instrument code) for a given symbol.
    Saxo doesn't use Yahoo-style tickers — orders require a Uic.
    Returns a list of possible matches (there can be more than one exchange
    listing for the same company name) — you pick the right one by checking
    the 'ExchangeId' / 'CurrencyCode' fields.

    IMPORTANT for the LIVE account: SIM and LIVE are very likely to assign
    different Uics for the same instrument. Never reuse a SIM-confirmed Uic
    for a LIVE order — call this with env="live" to re-derive it.
    """
    resp = _request_with_retry(
        "GET",
        f"{_base_url(env)}/ref/v1/instruments",
        headers=_headers(env),
        params={"Keywords": symbol, "AssetTypes": asset_type},
    )
    resp.raise_for_status()
    return resp.json().get("Data", [])


def post(path: str, body: dict, env: str = "sim") -> dict:
    """Generic POST for saxo_order.place_with_stop()'s post_fn parameter —
    lets stocks attach a native Saxo stop-loss/take-profit bracket the same
    way forex/futures/ETF already do, instead of a bare market order.
    Deliberately not wrapped in _request_with_retry — same reasoning as
    place_market_order: retrying an order POST after a timeout risks a
    duplicate fill if the first request actually reached Saxo.
    """
    resp = requests.post(f"{_base_url(env)}{path}", headers=_headers(env), json=body, timeout=30)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        body_text = resp.text.strip()
        raise requests.exceptions.HTTPError(f"{e} | Saxo response body: {body_text}", response=resp) from e
    return resp.json()


def get_quote(uic: int, asset_type: str, env: str = "sim") -> float | None:
    """Live mid-price for any instrument via /trade/v1/infoprices.

    Retries up to 3 times on 429 rate-limit or transient errors. Returns None
    on persistent failure so callers can fall back gracefully — the failure
    reason is printed to stderr so it appears in watchdog/scheduler logs.
    """
    import sys as _sys
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            params = {"Uic": uic, "AssetType": asset_type, "FieldGroups": "Quote"}
            resp = _request_with_retry("GET", f"{_base_url(env)}/trade/v1/infoprices",
                                       headers=_headers(env), params=params)
            if resp.status_code == 429:
                wait = 3 * (attempt + 1)
                print(f"  [get_quote] 429 rate-limit UIC={uic} env={env} — retry in {wait}s",
                      file=_sys.stderr, flush=True)
                time.sleep(wait)
                last_exc = RuntimeError(f"429 rate-limit UIC={uic}")
                continue
            resp.raise_for_status()
            q = resp.json().get("Quote", {})
            mid = q.get("Mid")
            if mid is None and q.get("Ask") and q.get("Bid"):
                mid = (float(q["Ask"]) + float(q["Bid"])) / 2.0
            return float(mid) if mid else None
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(1 * (attempt + 1))
    print(f"  [get_quote] FAILED UIC={uic} asset={asset_type} env={env}: {last_exc}",
          file=_sys.stderr, flush=True)
    return None


def get_orders(asset_type: str | None = None, env: str = "sim") -> dict:
    """Returns all working orders on the given account (optionally filtered
    to one AssetType). Used by housekeeping.py to reconcile stop/limit
    orders against live positions across every module."""
    params = {"AssetType": asset_type} if asset_type else None
    resp = _request_with_retry("GET", f"{_base_url(env)}/port/v1/orders/me",
                               headers=_headers(env), params=params)
    resp.raise_for_status()
    return resp.json()


def get_closed_positions(env: str = "sim") -> dict:
    """Returns recently closed positions (Saxo's own limited retention
    window) — used by housekeeping.py to explain why a locally-tracked
    position no longer has any live backing."""
    resp = _request_with_retry("GET", f"{_base_url(env)}/port/v1/closedpositions/me",
                               headers=_headers(env))
    resp.raise_for_status()
    return resp.json()


def cancel_order(order_id: str, env: str = "sim") -> bool:
    """Cancels a working order. Returns True on success, including if it's
    already gone (a 404 means nothing left to double-protect against, not
    a failure worth stopping for)."""
    try:
        resp = requests.delete(f"{_base_url(env)}/trade/v2/orders/{order_id}",
                               headers=_headers(env),
                               params={"AccountKey": get_account_key(env=env)}, timeout=15)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return True
        return False
    except Exception:
        return False


_decimals_cache: dict = {}


def get_price_decimals(uic: int, asset_type: str, env: str = "sim") -> int | None:
    """Live Format.Decimals lookup for a specific uic/asset_type, via
    /ref/v1/instruments/details. Needed whenever a symbol's own precision
    can't be assumed from its AssetType alone -- e.g. a futures-module
    symbol like GC/CADMXN whose real Saxo AssetType is FxSpot (Saxo's
    generic FxSpot default is 5dp) but whose actual required precision is
    2dp (GC/XAUUSD) or 4dp (CADMXN), not 5. Confirmed live 2026-08-24: a
    generic 5dp guess for both triggered a real PriceNotInTickSizeIncrements
    rejection. Cached per (uic, asset_type) for the life of the process --
    an instrument's own decimal precision doesn't change at runtime.
    Returns None (caller should fall back to its own default) if the
    lookup itself fails."""
    key = (uic, asset_type)
    if key in _decimals_cache:
        return _decimals_cache[key]
    try:
        resp = _request_with_retry("GET", f"{_base_url(env)}/ref/v1/instruments/details",
                                   headers=_headers(env),
                                   params={"Uics": str(uic), "AssetType": asset_type})
        resp.raise_for_status()
        data = resp.json().get("Data", [])
        if not data:
            return None
        fmt = data[0].get("Format") or {}
        dp = fmt.get("OrderDecimals") or fmt.get("Decimals")
        dp = int(dp) if dp is not None else None
        _decimals_cache[key] = dp
        return dp
    except Exception:
        return None


_tick_size_cache: dict = {}


def get_tick_size(uic: int, asset_type: str, env: str = "sim") -> float | None:
    """Live TickSize lookup for a specific uic/asset_type, via the same
    /ref/v1/instruments/details endpoint as get_price_decimals(). Needed
    because decimal PLACES and tick SIZE are not the same thing for
    exchange-listed futures: ZC (corn) reports Format.Decimals=2 but its
    real TickSize is 0.25, so rounding a stop price to 2 decimal places
    (e.g. 494.48) does NOT land on a valid tick and Saxo rejects it with
    PriceNotInTickSizeIncrements. Confirmed live 2026-08-24 on ZC's first
    real trade (the market this instrument became tradeable for the same
    day the capital cap was raised -- see futures_capital_cap_raised).
    Cached per (uic, asset_type) for the life of the process. Returns
    None (caller should fall back to decimal-place rounding) if the
    lookup fails or the instrument has no TickSize (most FX/CFD types
    round cleanly by decimal places alone and don't need this)."""
    key = (uic, asset_type)
    if key in _tick_size_cache:
        return _tick_size_cache[key]
    try:
        resp = _request_with_retry("GET", f"{_base_url(env)}/ref/v1/instruments/details",
                                   headers=_headers(env),
                                   params={"Uics": str(uic), "AssetType": asset_type})
        resp.raise_for_status()
        data = resp.json().get("Data", [])
        if not data:
            return None
        tick = data[0].get("TickSize")
        tick = float(tick) if tick is not None else None
        _tick_size_cache[key] = tick
        return tick
    except Exception:
        return None


def place_market_order(uic: int, asset_type: str, buy_sell: str, amount: int, env: str = "sim") -> dict:
    """
    Places a MARKET order on the given account. buy_sell must be 'Buy' or
    'Sell'. On env="sim" this is simulated money; on env="live" THIS IS A
    REAL ORDER WITH REAL MONEY.
    """
    order = {
        "AccountKey": get_account_key(env=env),
        "Uic": uic,
        "AssetType": asset_type,
        "BuySell": buy_sell,
        "Amount": amount,
        "OrderType": "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        # Saxo now requires this on every order. False because this bot
        # places orders algorithmically, not a human clicking buy/sell in
        # SaxoTraderGO — matters for Saxo's own compliance/reporting, not
        # anything this bot needs to reason about.
        "ManualOrder": False,
    }
    # NOTE: deliberately NOT wrapped in _request_with_retry. Retrying an
    # order after a timeout risks a duplicate fill (the first request may
    # have reached Saxo). A hard timeout is still required so a hung
    # connection can't stall the whole daily cycle indefinitely.
    resp = requests.post(f"{_base_url(env)}/trade/v2/orders", headers=_headers(env),
                         json=order, timeout=30)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        # requests' default message ("400 Client Error: Bad Request for
        # url: ...") drops the response body, which is exactly where Saxo
        # puts the actual rejection reason. Without this, live_order_log.csv
        # tells you an order failed but never why.
        body = resp.text.strip()
        raise requests.exceptions.HTTPError(f"{e} | Saxo response body: {body}", response=resp) from e
    return resp.json()


if __name__ == "__main__":
    print("Testing Saxo SIM connection...")
    try:
        user_info = test_connection()
        print("SUCCESS. Connected as:", user_info.get("UserId", "(no UserId returned)"))
        print("\nFetching account info...")
        accounts = get_account_info()
        print(accounts)
    except Exception as e:
        print("FAILED:", e)