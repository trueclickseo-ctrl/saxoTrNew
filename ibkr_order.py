"""
ibkr_order.py
--------------
IBKR equivalent of saxo_order.py -- places a Market entry with a native
stop-loss (and optional take-profit) attached, so IBKR enforces them
server-side even when the local machine is off, same guarantee Saxo's
native orders give you.

place_with_stop() keeps the same call shape as saxo_order.place_with_stop()
(uic, asset_type, amount, buy_sell, stop_price, label, take_profit_price,
symbol) so call sites mostly need an import swap -- but see the two real
differences below before wiring this in.

WHAT'S DIFFERENT FROM saxo_order.py
-------------------------------------
1. No `post_fn` parameter. Saxo's version takes an injected HTTP-post
   callable because it's building raw JSON request bodies. IBKR bracket
   orders are native Order objects with parentId/transmit linking instead
   of a JSON tree, so there's nothing to inject -- this module talks to
   ibkr_client's shared connection directly.

2. `account_key` is accepted for call-site compatibility but unused --
   IBKR's placeOrder() is already scoped to whichever account IB
   Gateway/TWS is logged into. Passing the wrong one won't do anything
   silently wrong; it just won't route the order elsewhere, unlike a
   misrouted AccountKey easily could on Saxo.

Everything else -- the asset-type -> duration/price-precision/stop-type
rules, JPY 3dp handling, long-only close-side logic, bracket-with-fallback
behaviour -- is ported as-is from saxo_order.py so the two behave the same
way from a risk-management standpoint.
"""

from __future__ import annotations

import logging

import ibkr_client

logger = logging.getLogger("ibkr_order")

try:
    from ib_async import StopOrder, StopLimitOrder, LimitOrder, MarketOrder  # type: ignore
except ImportError:
    from ib_insync import StopOrder, StopLimitOrder, LimitOrder, MarketOrder  # type: ignore


# ── Per-asset-type rules (ported verbatim from saxo_order.py) ─────────────────

_DURATION_TIF: dict[str, str] = {
    "FxSpot":          "GTC",
    "CfdOnIndex":      "GTC",
    "ContractFutures": "DAY",
    "CdfOnEtf":        "GTC",
    "Etf":             "GTC",
    "Stock":           "GTC",
    "CfdOnStock":      "GTC",
}

_PRICE_DP: dict[str, int] = {
    "FxSpot":          5,
    "CfdOnIndex":      2,
    "ContractFutures": 4,
    "CdfOnEtf":        4,
    "Etf":             2,
    "Stock":           2,
    "CfdOnStock":      2,
}

_JPY_SUFFIX = "JPY"
_LONG_ONLY: set[str] = {"Etf", "Stock"}
_STOP_LIMIT_TYPES: set[str] = {
    "CfdOnIndex", "ContractFutures", "CdfOnEtf", "CfdOnEtf",
    "Etf", "Stock", "CfdOnStock",
}


def _round_price(price: float, asset_type: str, symbol: str = "") -> float:
    dp = _PRICE_DP.get(asset_type, 5)
    if asset_type == "FxSpot" and symbol.upper().endswith(_JPY_SUFFIX):
        dp = 3
    return round(price, dp)


def _close_side(buy_sell: str, asset_type: str) -> str:
    if asset_type in _LONG_ONLY:
        return "SELL"
    return "SELL" if buy_sell.upper() == "BUY" else "BUY"


def _uses_stop_limit(asset_type: str) -> bool:
    return asset_type in _STOP_LIMIT_TYPES


def _stop_limit_price(order_price: float, close_side: str, asset_type: str,
                       symbol: str = "") -> float:
    dp = _PRICE_DP.get(asset_type, 2)
    if asset_type == "FxSpot" and symbol.upper().endswith(_JPY_SUFFIX):
        dp = 3
    if close_side == "SELL":
        return round(order_price * 0.99, dp)
    return round(order_price * 1.01, dp)


# ── Public entry point ─────────────────────────────────────────────────────

def place_with_stop(
    account_key: str,
    uic: int,
    asset_type: str,
    amount: int,
    buy_sell: str,
    stop_price: float,
    label: str = "",
    take_profit_price: float | None = None,
    symbol: str = "",
) -> tuple:
    """
    Place a Market entry + native IBKR stop-loss (and optional take-profit).

    Parameters mirror saxo_order.place_with_stop() exactly except there is
    no post_fn (see module docstring). `uic` is the conId returned by
    ibkr_client.find_instrument(), not a Saxo Uic.

    Returns
    -------
    (entry_order_id, stop_order_id, tp_order_id)
        tp_order_id is None when no take-profit was requested.
    """
    ib = ibkr_client._client()
    contract = ibkr_client._resolve_by_conid(uic)

    buy_sell = buy_sell.upper()
    close = _close_side(buy_sell, asset_type)
    tif = _DURATION_TIF.get(asset_type, "GTC")
    rstop = _round_price(stop_price, asset_type, symbol)
    use_stop_limit = _uses_stop_limit(asset_type)
    slp = _stop_limit_price(rstop, close, asset_type, symbol) if use_stop_limit else None

    # IBKR native bracket: entry (parent) + stop-loss + take-profit (children),
    # linked via parentId with transmit=False on the parent/first child and
    # transmit=True on the last -- IBKR only submits the whole group once the
    # final order in the chain transmits. This is the direct equivalent of
    # Saxo's IfDoneSlave / OCO relation.
    parent = MarketOrder(buy_sell, amount)
    parent.orderId = ib.client.getReqId()
    parent.transmit = False

    if use_stop_limit:
        stop_leg = StopLimitOrder(close, amount, stopPrice=rstop, lmtPrice=slp)
    else:
        stop_leg = StopOrder(close, amount, stopPrice=rstop)
    stop_leg.orderId = ib.client.getReqId()
    stop_leg.parentId = parent.orderId
    stop_leg.tif = tif
    stop_leg.transmit = take_profit_price is None   # transmit now only if no TP leg follows

    legs = [parent, stop_leg]

    tp_leg = None
    if take_profit_price is not None:
        rtp = _round_price(take_profit_price, asset_type, symbol)
        tp_leg = LimitOrder(close, amount, rtp)
        tp_leg.orderId = ib.client.getReqId()
        tp_leg.parentId = parent.orderId
        tp_leg.tif = tif
        tp_leg.transmit = True   # last leg transmits the whole bracket
        legs.append(tp_leg)

    try:
        trades = [ib.placeOrder(contract, o) for o in legs]
        entry_oid = str(parent.orderId)
        stop_oid = str(stop_leg.orderId)
        tp_oid = str(tp_leg.orderId) if tp_leg is not None else None
        logger.info(
            "[bracket] %s entry=%s %s stop@%s%s stop_id=%s tp_id=%s",
            label, entry_oid, "StopLimit" if use_stop_limit else "Stop", rstop,
            f" Limit tp@{take_profit_price}" if take_profit_price else "",
            stop_oid, tp_oid,
        )
        return entry_oid, stop_oid, tp_oid
    except Exception as exc:
        logger.warning("[bracket] %s bracket order failed: %s", label, exc)
        raise


# ── Standalone stop/limit orders (healing -- attach a missing stop/TP to an
# already-filled entry, outside the atomic bracket above) ──────────────────

def place_stop(uic: int, asset_type: str, amount: int, buy_sell: str,
                stop_price: float, symbol: str = "") -> str:
    """Place a standalone GTC-equivalent stop order (no linked entry/TP) --
    used by healing logic (e.g. forex/runner.py's _heal_missing_stops()) to
    attach a stop to a position whose entry already filled without one.
    Returns the new order's id."""
    ib = ibkr_client._client()
    contract = ibkr_client._resolve_by_conid(uic)
    close = buy_sell.upper()
    tif = _DURATION_TIF.get(asset_type, "GTC")
    rstop = _round_price(stop_price, asset_type, symbol)

    if _uses_stop_limit(asset_type):
        slp = _stop_limit_price(rstop, close, asset_type, symbol)
        order = StopLimitOrder(close, amount, stopPrice=rstop, lmtPrice=slp)
    else:
        order = StopOrder(close, amount, stopPrice=rstop)
    order.tif = tif
    trade = ib.placeOrder(contract, order)
    return str(trade.order.orderId)


def place_limit(uic: int, asset_type: str, amount: int, buy_sell: str,
                 price: float, symbol: str = "") -> str:
    """Place a standalone GTC-equivalent limit order (e.g. a missing
    take-profit). Returns the new order's id."""
    ib = ibkr_client._client()
    contract = ibkr_client._resolve_by_conid(uic)
    tif = _DURATION_TIF.get(asset_type, "GTC")
    rprice = _round_price(price, asset_type, symbol)
    order = LimitOrder(buy_sell.upper(), amount, rprice)
    order.tif = tif
    trade = ib.placeOrder(contract, order)
    return str(trade.order.orderId)


def amend_stop(order_id: str, new_stop_price: float, symbol: str = "",
                asset_type: str = "FxSpot") -> bool:
    """Amend an existing open stop order's trigger price in place (IBKR
    resubmits under the same orderId to modify rather than a REST PATCH,
    which is what saxo_order-based callers used) -- the direct equivalent of
    Saxo's PATCH /trade/v2/orders/{id} used for breakeven-stop moves.

    Returns False if the order isn't found among currently open orders
    (already filled/cancelled, or -- same limitation as
    ibkr_client.get_open_orders() -- placed in a different session)."""
    ib = ibkr_client._client()
    target = None
    for t in ib.openTrades():
        if str(t.order.orderId) == str(order_id):
            target = t
            break
    if target is None:
        return False

    rstop = _round_price(new_stop_price, asset_type, symbol)
    target.order.auxPrice = rstop
    if target.order.orderType == "STP LMT":
        close = target.order.action
        target.order.lmtPrice = _stop_limit_price(rstop, close, asset_type, symbol)
    ib.placeOrder(target.contract, target.order)
    return True
