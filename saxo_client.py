"""
saxo_client.py
--------------
Handles all communication with Saxo's OpenAPI Simulation (SIM) environment.

SECURITY: the access token is read from an environment variable, never
hardcoded or pasted into this file. See README_SAXO_SETUP.md for how to
set it on your machine.
"""

import os
import time
import requests
import saxo_auth

SIM_BASE_URL = "https://gateway.saxobank.com/sim/openapi"

MAX_RETRIES = 4
RETRY_DELAY_SECONDS = 5


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


def get_token() -> str:
    """
    Prefers the self-refreshing PKCE login (saxo_auth.py) so the bot can run
    unattended. Falls back to the manual 24h SAXO_TOKEN env var if no PKCE
    login has been done yet — handy for quick one-off tests.
    """
    try:
        return saxo_auth.get_valid_access_token()
    except RuntimeError:
        token = os.environ.get("SAXO_TOKEN")
        if not token:
            raise RuntimeError(
                "No Saxo login available. Either run `python saxo_auth.py` once "
                "to log in via PKCE (recommended — self-refreshing), or set the "
                "SAXO_TOKEN env var with a 24h token for a quick manual test. "
                "See README_SAXO_SETUP.md."
            )
        return token


def _headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


def test_connection() -> dict:
    """
    Calls Saxo's 'who am I' endpoint — the simplest possible test that the
    token is valid and the connection works. Returns basic user info.
    """
    resp = _request_with_retry("GET", f"{SIM_BASE_URL}/port/v1/users/me", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def get_account_info() -> dict:
    """Returns account details, including the AccountKey needed for orders."""
    resp = _request_with_retry("GET", f"{SIM_BASE_URL}/port/v1/accounts/me", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def get_positions() -> dict:
    """Returns currently open positions on the SIM account."""
    resp = _request_with_retry("GET", f"{SIM_BASE_URL}/port/v1/positions/me", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def get_balances() -> dict:
    """
    Returns account balance data, including TotalValue (equity) and
    CashAvailableForTrading — this is the source of truth for equity/cash,
    not anything computed locally.
    """
    resp = _request_with_retry("GET", f"{SIM_BASE_URL}/port/v1/balances/me", headers=_headers())
    resp.raise_for_status()
    return resp.json()


_account_key_cache = None


def get_account_key() -> str:
    """AccountKey needed to place orders. Prefer the SAXO_ACCOUNT_KEY env var;
    otherwise fetch it from the API once and cache it for this process (the SIM
    login has a single account), so orders work out of the box."""
    global _account_key_cache
    key = os.environ.get("SAXO_ACCOUNT_KEY")
    if key:
        return key
    if _account_key_cache:
        return _account_key_cache
    info = get_account_info()
    data = info.get("Data", info)
    acct = data[0] if isinstance(data, list) and data else data
    key = acct.get("AccountKey") if isinstance(acct, dict) else None
    if not key:
        raise RuntimeError(
            "Could not determine AccountKey from Saxo /port/v1/accounts/me. "
            "Set the SAXO_ACCOUNT_KEY env var explicitly."
        )
    _account_key_cache = key
    return key


def find_instrument(symbol: str, asset_type: str = "Stock") -> list[dict]:
    """
    Looks up Saxo's internal Uic (instrument code) for a given symbol.
    Saxo doesn't use Yahoo-style tickers — orders require a Uic.
    Returns a list of possible matches (there can be more than one exchange
    listing for the same company name) — you pick the right one by checking
    the 'ExchangeId' / 'CurrencyCode' fields.
    """
    resp = _request_with_retry(
        "GET",
        f"{SIM_BASE_URL}/ref/v1/instruments",
        headers=_headers(),
        params={"Keywords": symbol, "AssetTypes": asset_type},
    )
    resp.raise_for_status()
    return resp.json().get("Data", [])


def post(path: str, body: dict) -> dict:
    """Generic POST for saxo_order.place_with_stop()'s post_fn parameter —
    lets stocks attach a native Saxo stop-loss/take-profit bracket the same
    way forex/futures/ETF already do, instead of a bare market order.
    Deliberately not wrapped in _request_with_retry — same reasoning as
    place_market_order: retrying an order POST after a timeout risks a
    duplicate fill if the first request actually reached Saxo.
    """
    resp = requests.post(f"{SIM_BASE_URL}{path}", headers=_headers(), json=body, timeout=30)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        body_text = resp.text.strip()
        raise requests.exceptions.HTTPError(f"{e} | Saxo response body: {body_text}", response=resp) from e
    return resp.json()


def get_orders(asset_type: str | None = None) -> dict:
    """Returns all working orders on the SIM account (optionally filtered to
    one AssetType). Used by housekeeping.py to reconcile stop/limit orders
    against live positions across every module."""
    params = {"AssetType": asset_type} if asset_type else None
    resp = _request_with_retry("GET", f"{SIM_BASE_URL}/port/v1/orders/me",
                               headers=_headers(), params=params)
    resp.raise_for_status()
    return resp.json()


def get_closed_positions() -> dict:
    """Returns recently closed positions (Saxo's own limited retention
    window) — used by housekeeping.py to explain why a locally-tracked
    position no longer has any live backing."""
    resp = _request_with_retry("GET", f"{SIM_BASE_URL}/port/v1/closedpositions/me",
                               headers=_headers())
    resp.raise_for_status()
    return resp.json()


def cancel_order(order_id: str) -> bool:
    """Cancels a live order. Returns True on success, including if it's
    already gone (a 404 means nothing left to double-protect against, not
    a failure worth stopping for)."""
    try:
        resp = requests.delete(f"{SIM_BASE_URL}/trade/v2/orders/{order_id}",
                               headers=_headers(),
                               params={"AccountKey": get_account_key()}, timeout=15)
        resp.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return True
        return False
    except Exception:
        return False


_decimals_cache: dict = {}


def get_price_decimals(uic: int, asset_type: str) -> int | None:
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
        resp = _request_with_retry("GET", f"{SIM_BASE_URL}/ref/v1/instruments/details",
                                   headers=_headers(),
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


def place_market_order(uic: int, asset_type: str, buy_sell: str, amount: int) -> dict:
    """
    Places a MARKET order on the SIM account. buy_sell must be 'Buy' or 'Sell'.
    THIS PLACES A REAL (SIMULATED) ORDER — no real money, but it will show
    up as an actual position on your SIM account.
    """
    order = {
        "AccountKey": get_account_key(),
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
    resp = requests.post(f"{SIM_BASE_URL}/trade/v2/orders", headers=_headers(),
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