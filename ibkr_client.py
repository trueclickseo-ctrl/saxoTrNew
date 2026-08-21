"""
ibkr_client.py
---------------
Handles all communication with Interactive Brokers via IB Gateway / TWS
(TWS API, using the ib_insync / ib_async wrapper).

Deliberately mirrors saxo_client.py's function names and call shapes
(test_connection, get_account_info, get_positions, get_balances,
get_account_key, find_instrument, place_market_order) so migrating a
call site is close to a rename, not a rewrite. Read
"WHAT'S DIFFERENT FROM saxo_client.py" below before wiring this in --
one identifier concept genuinely doesn't translate 1:1.

WHAT'S DIFFERENT FROM saxo_client.py
-------------------------------------
Saxo identifies instruments by an internal integer Uic, looked up once via
find_instrument() and then cached everywhere (instrument_map.csv,
futures_uic_cache.json, etc). IBKR has no equivalent integer you look up
in advance -- it resolves a Contract from symbol + exchange + currency +
asset type, and *that* resolution is what produces IBKR's own internal id
(conId).

So: find_instrument() below returns the same list-of-dicts shape Saxo's
version does, but the "Uic" key holds IBKR's conId instead of a Saxo Uic.
Anywhere your code currently does `imap[ticker]["uic"]` or reads
`Data[0]["Identifier"]` from find_instrument(), it will keep working
almost unchanged -- it's now holding a conId, not a Uic, but every function
below (place_market_order, etc.) accepts that same conId back in, so the
round-trip is consistent. What does NOT carry over automatically is
instrument_map.csv itself -- it has Saxo Uics baked in, so it needs to be
rebuilt for IBKR conIds (a lookup_instruments_ibkr.py-style script) before
any code that reads that CSV will work against this client.

Auth model: no token, no API key/secret. This connects a TCP socket to an
already-running, already-logged-in IB Gateway or TWS process -- see
README_IBKR_SETUP.md for one-time setup. There is nothing in this file
analogous to saxo_auth.py's PKCE flow, because there's nothing to log
into from Python.
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

MAX_RETRIES = 4
RETRY_DELAY_SECONDS = 5

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4002        # IB Gateway, PAPER. Live paper/live ports differ -- see README.
DEFAULT_CLIENT_ID = 1

# Saxo AssetType strings -> IBKR (secType, exchange, extra) resolution rules.
# CfdOnIndex / CdfOnEtf / CfdOnStock have no clean IBKR equivalent in most
# regions (IBKR CFDs are a separate, narrower product set) -- these map to
# best-effort placeholders and are flagged loudly if actually used.
_ASSET_TYPE_SECTYPE = {
    "Stock":           "STK",
    "CfdOnStock":       "STK",   # falls back to the underlying stock, not a CFD
    "Etf":             "STK",    # IBKR treats ETFs as STK contracts
    "CdfOnEtf":         "STK",   # falls back to the underlying ETF, not a CFD
    "FxSpot":          "CASH",
    "ContractFutures":  "FUT",
    "CfdOnIndex":       "IND",   # falls back to the index itself (data only,
                                 # NOT tradable as CFD -- see README)
}

_FOREX_PAIR_RE = re.compile(r"^[A-Z]{6}$")

# ib_async is the actively-maintained fork of the unmaintained ib_insync;
# both expose an identical API. Optional import, same reasoning as
# saxo_client.py needing saxo_auth importable but not hard-required at
# module load for every caller.
try:
    from ib_async import IB, Stock, Forex, Future, Index, Contract, MarketOrder  # type: ignore
    _SDK_NAME = "ib_async"
except ImportError:
    try:
        from ib_insync import IB, Stock, Forex, Future, Index, Contract, MarketOrder  # type: ignore
        _SDK_NAME = "ib_insync"
    except ImportError:
        raise ImportError(
            "Neither ib_async nor ib_insync is installed. "
            "Run: pip install ib_async"
        )


_ib: Optional["IB"] = None
_account_key_cache: Optional[str] = None
_contract_cache: dict[tuple, "Contract"] = {}


def _client() -> "IB":
    """Return the module-level IB connection, connecting on first use.

    Mirrors saxo_client.get_token()'s "connect/refresh transparently so
    callers never think about it" role -- callers here never construct an
    IB() themselves, same as they never touch saxo_auth.py directly.
    """
    global _ib
    if _ib is not None and _ib.isConnected():
        return _ib

    host = os.environ.get("IBKR_HOST", DEFAULT_HOST)
    port = int(os.environ.get("IBKR_PORT", DEFAULT_PORT))
    client_id = int(os.environ.get("IBKR_CLIENT_ID", DEFAULT_CLIENT_ID))

    _ib = IB()
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            _ib.connect(host, port, clientId=client_id, timeout=15)
            # IBKR defaults every new connection to live (type 1) market
            # data, which returns nothing at all unless the account has a
            # paid real-time subscription for that instrument -- true for
            # most paper accounts and plenty of live ones. Delayed (type 3)
            # is free and "just works" without one; set IBKR_MARKET_DATA_TYPE
            # to 1 once you have real-time entitlements for what you trade.
            md_type = int(os.environ.get("IBKR_MARKET_DATA_TYPE", 3))
            _ib.reqMarketDataType(md_type)
            return _ib
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                delay = RETRY_DELAY_SECONDS * attempt
                print(f"    IBKR connect failed ({e.__class__.__name__}), retrying in {delay}s...")
                time.sleep(delay)
    raise RuntimeError(
        f"Could not connect to IB Gateway/TWS at {host}:{port}. "
        f"Make sure IB Gateway/TWS is running, logged in, and API access is "
        f"enabled (Settings > API > Enable Socket Clients). "
        f"Underlying error: {last_error}"
    )


def test_connection() -> dict:
    """
    Simplest possible test that the connection works -- mirrors
    saxo_client.test_connection()'s 'who am I' role. IBKR has no
    equivalent single endpoint, so this returns the managed account list,
    which is the closest thing to 'who am I logged in as'.
    """
    ib = _client()
    accounts = ib.managedAccounts()
    return {"UserId": accounts[0] if accounts else None, "Accounts": accounts}


def get_account_info() -> dict:
    """Returns {'AccountKey': <IBKR account id>} -- the id needed for
    account-scoped calls, playing the same role as Saxo's AccountKey."""
    return {"AccountKey": get_account_key()}


def get_account_key() -> str:
    """
    IBKR account id (e.g. 'DU1234567' for paper). Prefer the
    IBKR_ACCOUNT_ID env var; otherwise take the first managed account,
    caching it for this process -- same pattern as
    saxo_client.get_account_key()'s SAXO_ACCOUNT_KEY / SIM-single-account
    fallback.
    """
    global _account_key_cache
    key = os.environ.get("IBKR_ACCOUNT_ID")
    if key:
        return key
    if _account_key_cache:
        return _account_key_cache
    ib = _client()
    accounts = ib.managedAccounts()
    if not accounts:
        raise RuntimeError(
            "Could not determine an IBKR account id from managedAccounts(). "
            "Set the IBKR_ACCOUNT_ID env var explicitly."
        )
    _account_key_cache = accounts[0]
    return _account_key_cache


def get_positions() -> dict:
    """
    Returns currently open positions, in a simplified (not byte-identical
    to Saxo's nested JSON) shape:

        {"Data": [
            {"Uic": <conId>, "Symbol": "AAPL", "AssetType": "Stock",
             "Amount": 10, "AverageCost": 227.50, "Currency": "USD"},
            ...
        ]}

    Note this is a flattened re-shaping, not a field-for-field match of
    Saxo's /port/v1/positions/me response -- any code that currently
    reaches deep into Saxo's PositionBase/PositionView nesting will need
    updating to this flatter shape, not just a function-name swap.
    """
    ib = _client()
    rows = []
    for p in ib.positions():
        if p.position == 0:
            continue
        rows.append({
            "Uic": p.contract.conId,
            "Symbol": p.contract.symbol,
            "AssetType": _sectype_to_asset_type(p.contract.secType),
            "Amount": p.position,
            "AverageCost": p.avgCost,
            "Currency": p.contract.currency,
        })
    return {"Data": rows}


def get_balances() -> dict:
    """
    Returns account balance data in a simplified shape:
        {"TotalValue": <equity>, "CashAvailableForTrading": <free cash>,
         "CashBalance": <free cash>, "Currency": "USD"}

    Same "source of truth, not computed locally" principle as
    saxo_client.get_balances() -- pulled straight from accountSummary().

    "CashBalance" duplicates "CashAvailableForTrading" -- Saxo's real
    /port/v1/balances/me response has both as genuinely different figures
    (CashBalance ignores margin reservations from open positions,
    CashAvailableForTrading doesn't), but every call site in this codebase
    that reads "CashBalance" is using it as "cash I can spend right now",
    which AvailableFunds is the correct IBKR figure for. Some of those call
    sites (saxo_live_engine.py, dashboard.py) use plain `balances["CashBalance"]`
    rather than `.get(...)`, so omitting the key would raise KeyError, not
    silently return 0 -- keeping both keys is what makes the "just swap the
    import" migration path actually true instead of a hunt through every
    balance-reading call site in the codebase.
    """
    ib = _client()
    rows = {r.tag: r for r in ib.accountSummary()}
    net_liq = rows.get("NetLiquidation")
    avail = rows.get("AvailableFunds")
    if net_liq is None or avail is None:
        raise RuntimeError(
            "NetLiquidation/AvailableFunds not yet available from IBKR "
            "accountSummary() -- this can happen right after connect() "
            "before the account snapshot has loaded. Retry after ~1-2s."
        )
    return {
        "TotalValue": float(net_liq.value),
        "CashAvailableForTrading": float(avail.value),
        "CashBalance": float(avail.value),
        "Currency": net_liq.currency,
    }


def find_instrument(symbol: str, asset_type: str = "Stock") -> list[dict]:
    """
    Resolves `symbol` to IBKR contract(s), mirroring
    saxo_client.find_instrument()'s role of "look up the id you need to
    place orders." Returns a list of dicts (usually just one match) shaped
    like Saxo's response:

        [{"Uic": <conId>, "Identifier": <conId>, "Symbol": "AAPL",
          "Description": "APPLE INC", "ExchangeId": "SMART",
          "CurrencyCode": "USD", "AssetType": "Stock"}]

    symbol accepts:
      "AAPL"           plain ticker -> Stock, SMART, default currency
      "EURUSD"         6-letter A-Z -> treated as an FX pair (Forex)
      explicit override: pass asset_type="ContractFutures" etc. for the
      Saxo-style asset types above; for anything not resolvable this way
      (specific futures expiries, non-US listings), qualify the Contract
      yourself and skip find_instrument().
    """
    ib = _client()
    contract = _build_contract(symbol, asset_type)

    # Futures given without a pinned expiry (the common case -- plain "ES",
    # or "ES:FUT:CME:USD") are ambiguous across contract months.
    # qualifyContracts() doesn't resolve these; it logs an "Ambiguous
    # contract" error and leaves the slot unresolved (None), which used to
    # crash this function outright. reqContractDetails() + picking the
    # nearest not-yet-expired month is the actual fix.
    if contract.secType == "FUT" and not contract.lastTradeDateOrContractMonth:
        return _resolve_future(ib, contract, asset_type)

    qualified = ib.qualifyContracts(contract)
    results = []
    for c in qualified:
        if c is None or not getattr(c, "conId", None):
            continue   # unresolved/ambiguous slot -- skip, don't crash
        results.append({
            "Uic": c.conId,
            "Identifier": c.conId,
            "Symbol": c.symbol,
            "Description": getattr(c, "description", "") or getattr(c, "localSymbol", c.symbol),
            "ExchangeId": c.exchange,
            "CurrencyCode": c.currency,
            "AssetType": asset_type,
        })
    return results


def _resolve_future(ib: "IB", contract: "Contract", asset_type: str) -> list[dict]:
    """Resolve an unpinned futures contract to its nearest not-yet-expired
    month via reqContractDetails(). Still needs a human contract-month check
    before trading (lookup_instruments_ibkr.py flags every futures row for
    that regardless) -- this just makes lookup not fail outright."""
    try:
        details = ib.reqContractDetails(contract)
    except Exception:
        return []
    today = time.strftime("%Y%m%d")
    candidates = sorted(
        (d.contract for d in details
         if d.contract.lastTradeDateOrContractMonth and d.contract.lastTradeDateOrContractMonth >= today),
        key=lambda c: c.lastTradeDateOrContractMonth,
    )
    if not candidates:
        return []
    c = candidates[0]
    return [{
        "Uic": c.conId,
        "Identifier": c.conId,
        "Symbol": c.symbol,
        "Description": c.localSymbol or c.symbol,
        "ExchangeId": c.exchange,
        "CurrencyCode": c.currency,
        "AssetType": asset_type,
    }]


def place_market_order(uic: int, asset_type: str, buy_sell: str, amount: int) -> dict:
    """
    Places a MARKET order. Same signature shape as
    saxo_client.place_market_order(uic, asset_type, buy_sell, amount) --
    `uic` here is the conId returned by find_instrument(), not a Saxo Uic.

    Returns {"OrderId": <id>}, matching Saxo's minimal success response
    shape, plus "Status" for convenience (Saxo callers that only read
    OrderId are unaffected).
    """
    ib = _client()
    contract = _resolve_by_conid(uic)
    order = MarketOrder(buy_sell, amount)
    trade = ib.placeOrder(contract, order)

    # Wait briefly for a definitive first status, same reasoning as
    # saxo_client's hard timeout on order placement -- bounded, not retried
    # (retrying a timed-out order risks a duplicate fill).
    deadline = time.time() + 10
    while time.time() < deadline:
        ib.sleep(0.25)
        if trade.orderStatus.status not in ("PendingSubmit", "PreSubmitted", ""):
            break

    return {"OrderId": str(trade.order.orderId), "Status": trade.orderStatus.status}


def place_order(uic: int, side: str, qty: int, asset_type: str = "Stock") -> dict:
    """
    Alias for place_market_order with the (uic, side, qty, asset_type)
    keyword shape some call sites use -- same underlying call.
    """
    return place_market_order(uic=uic, asset_type=asset_type, buy_sell=side, amount=qty)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _build_contract(symbol: str, asset_type: str) -> "Contract":
    # Explicit "SYMBOL:SECTYPE:EXCHANGE:CURRENCY" form, e.g. "ES:FUT:CME:USD"
    # or "VOD:STK:LSE:GBP" -- this is what lookup_instruments_ibkr.py sends
    # for every non-US stock and every futures contract, since plain-ticker
    # resolution can't pin those to the right exchange/currency on its own.
    parts = symbol.split(":")
    if len(parts) == 4:
        sym, sec, exch, curr = parts
        if sec == "FUT":
            return Future(sym, exchange=exch, currency=curr)
        if sec == "CASH":
            return Forex(sym)
        if sec == "IND":
            return Index(sym, exchange=exch, currency=curr)
        return Stock(sym, exch, curr)

    sec_type = _ASSET_TYPE_SECTYPE.get(asset_type)
    if sec_type is None:
        raise ValueError(
            f"Unrecognised asset_type '{asset_type}'. Known: "
            f"{sorted(_ASSET_TYPE_SECTYPE)}"
        )
    currency = os.environ.get("IBKR_CURRENCY", "USD")

    if sec_type == "CASH" or _FOREX_PAIR_RE.match(symbol):
        return Forex(symbol)
    if sec_type == "FUT":
        return Future(symbol, exchange=os.environ.get("IBKR_FUT_EXCHANGE", "CME"), currency=currency)
    if sec_type == "IND":
        return Index(symbol, exchange=os.environ.get("IBKR_IND_EXCHANGE", "CBOE"), currency=currency)
    return Stock(symbol, "SMART", currency)


def _resolve_by_conid(conid: int) -> "Contract":
    if conid in _contract_cache:
        return _contract_cache[conid]
    ib = _client()
    c = Contract(conId=conid)
    qualified = ib.qualifyContracts(c)
    if not qualified:
        raise RuntimeError(
            f"Could not resolve conId={conid} to a contract. "
            f"It may be stale (from a previous session) -- re-run "
            f"find_instrument() to get a fresh conId."
        )
    _contract_cache[conid] = qualified[0]
    return qualified[0]


def _sectype_to_asset_type(sec_type: str) -> str:
    for asset_type, st in _ASSET_TYPE_SECTYPE.items():
        if st == sec_type:
            return asset_type
    return sec_type


if __name__ == "__main__":
    print("Testing IBKR connection (IB Gateway/TWS must already be running)...")
    try:
        info = test_connection()
        print("SUCCESS. Connected as:", info.get("UserId", "(no account returned)"))
        print("\nFetching account info...")
        print(get_account_info())
        print("\nFetching balances...")
        print(get_balances())
    except Exception as e:
        print("FAILED:", e)
