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

from atos.logger import get_logger
logger = get_logger("saxo_client")

SIM_BASE_URL = "https://gateway.saxobank.com/sim/openapi"

MAX_RETRIES = 4
RETRY_DELAY_SECONDS = 5


def _request_with_retry(method: str, url: str, **kwargs):
    """
    Wraps requests calls with retry logic for transient network/SSL errors,
    which happen occasionally with any API and aren't a sign of anything
    wrong with your setup.
    """
    logger.debug("API %s %s", method, url)
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
                logger.warning("API retry %d/%d for %s: %s", attempt, MAX_RETRIES, url, e.__class__.__name__)
                time.sleep(delay)
    logger.error("API request failed after %d retries: %s %s", MAX_RETRIES, method, url)
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
    resp = requests.get(f"{SIM_BASE_URL}/port/v1/users/me", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def get_account_info() -> dict:
    """Returns account details, including the AccountKey needed for orders."""
    resp = requests.get(f"{SIM_BASE_URL}/port/v1/accounts/me", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def get_positions() -> dict:
    """Returns currently open positions on the SIM account."""
    resp = requests.get(f"{SIM_BASE_URL}/port/v1/positions/me", headers=_headers())
    resp.raise_for_status()
    return resp.json()


def get_balances() -> dict:
    """
    Returns account balance data, including TotalValue (equity) and
    CashAvailableForTrading — this is the source of truth for equity/cash,
    not anything computed locally.
    """
    resp = requests.get(f"{SIM_BASE_URL}/port/v1/balances/me", headers=_headers())
    resp.raise_for_status()
    logger.debug("Balances fetched successfully")
    return resp.json()


def get_account_key() -> str:
    key = os.environ.get("SAXO_ACCOUNT_KEY")
    if not key:
        raise RuntimeError(
            "SAXO_ACCOUNT_KEY environment variable not set. Run get_account_info() "
            "once, copy the AccountKey from the output, and set it as an env var "
            "the same way you set SAXO_TOKEN."
        )
    return key


def find_instrument(symbol: str, asset_type: str = "Stock") -> list[dict]:
    """
    Looks up Saxo's internal Uic (instrument code) for a given symbol.
    Saxo doesn't use Yahoo-style tickers — orders require a Uic.
    Returns a list of possible matches (there can be more than one exchange
    listing for the same company name) — you pick the right one by checking
    the 'ExchangeId' / 'CurrencyCode' fields.
    """
    resp = requests.get(
        f"{SIM_BASE_URL}/ref/v1/instruments",
        headers=_headers(),
        params={"Keywords": symbol, "AssetTypes": asset_type},
    )
    resp.raise_for_status()
    return resp.json().get("Data", [])


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
    resp = requests.post(f"{SIM_BASE_URL}/trade/v2/orders", headers=_headers(), json=order)
    try:
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        # requests' default message ("400 Client Error: Bad Request for
        # url: ...") drops the response body, which is exactly where Saxo
        # puts the actual rejection reason. Without this, live_order_log.csv
        # tells you an order failed but never why.
        body = resp.text.strip()
        logger.error("Order rejected for UIC %d: %s", uic, body)
        raise requests.exceptions.HTTPError(f"{e} | Saxo response body: {body}", response=resp) from e
    logger.info("Order placed: %s %d units of UIC %d (%s)", buy_sell, amount, uic, asset_type)
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