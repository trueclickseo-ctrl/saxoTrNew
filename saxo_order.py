"""
saxo_order.py
-------------
Shared order utilities used by forex, futures, and ETF runners.

place_with_stop()
    Places a Market entry order and attaches native Saxo stop-loss and
    (optionally) take-profit orders.  Saxo enforces them 24/7, even when
    the local machine is off.

    Without take_profit_price  → separate GTC stop-loss order.
    With    take_profit_price  → bracket order (OCO): stop + limit sent
                                  together inside the entry order's Orders
                                  array.  When one fires Saxo cancels the
                                  other automatically.  Falls back to a
                                  separate stop-only if bracket is rejected.

Per-asset-type rules applied automatically:

  Asset type        Stop type   Close side  Duration        Price dp
  ──────────────────────────────────────────────────────────────────
  FxSpot            Stop        Sell/Buy    GoodTillCancel  5 (3 for JPY crosses)
  CfdOnIndex        StopLimit   Sell/Buy    GoodTillCancel  2
  ContractFutures   StopLimit   Sell/Buy    DayOrder        4
  CdfOnEtf          StopLimit   Sell/Buy    GoodTillCancel  2
  Etf               StopLimit   Sell only   GoodTillCancel  2
  Stock/CfdOnStock  StopLimit   Sell only   GoodTillCancel  2

Note: Saxo uses OrderType="Stop" with OrderPrice for FxSpot.
For exchange-listed instruments (Etf, Stock, CfdOnIndex, CdfOnEtf,
ContractFutures) only StopLimit is accepted.  StopLimitPrice is set
1% beyond OrderPrice to absorb slippage (sell: 1% below; buy: 1% above).
"""

import logging

logger = logging.getLogger("saxo_order")

# Set to the last entry-order rejection's error string (e.g. Saxo's
# "CouldNotCompleteRequest (90)" body) so the caller can surface WHY without
# re-parsing the log. Cleared to "" on a successful entry.
LAST_ENTRY_ERROR: str = ""

# ── Per-asset-type rules ───────────────────────────────────────────────────

# ContractFutures are exchange-listed — GTC is often rejected; use DayOrder.
_DURATION: dict[str, str] = {
    "FxSpot":          "GoodTillCancel",
    "CfdOnIndex":      "GoodTillCancel",
    "ContractFutures": "DayOrder",
    "CdfOnEtf":        "GoodTillCancel",
    "Etf":             "GoodTillCancel",
    "Stock":           "GoodTillCancel",
    "CfdOnStock":      "GoodTillCancel",
}

_PRICE_DP: dict[str, int] = {
    "FxSpot":          5,   # overridden per-symbol for JPY crosses (3dp)
    "CfdOnIndex":      2,
    "ContractFutures": 4,
    "CdfOnEtf":        4,
    "Etf":             2,
    "Stock":           2,
    "CfdOnStock":      2,
}

# FX pairs whose quote currency needs 3 dp instead of 5
_JPY_SUFFIX = "JPY"

# Long-only assets — stop/limit close side is always Sell.
_LONG_ONLY: set[str] = {"Etf", "Stock"}


def _round_price(price: float, asset_type: str, symbol: str = "",
                 price_decimals: int | None = None,
                 tick_size: float | None = None) -> float:
    """price_decimals, when given, is the INSTRUMENT'S OWN decimal precision
    (from Saxo's live Format.Decimals via forex.universe — see the note on
    place_with_stop). Overrides the generic FxSpot default/JPY-only guess
    below, which is only correct for the 5dp-majors/3dp-JPY cases it was
    written for — found 2026-08-21 via a live PriceNotInTickSizeIncrements
    rejection on AUDTRY (pip_size 0.001 = 4dp, not 5dp): the entry order
    still went through (Market orders don't carry a price), but the STOP
    order was rejected outright, leaving a real position open with NO
    stop-loss attached. Any TRY/CNH-quoted pair (4dp, not 5dp) hits this.

    tick_size, when given, takes priority over price_decimals entirely:
    decimal PLACES and tick SIZE are not the same thing for exchange-listed
    futures whose native tick is coarser than its own decimal precision —
    ZC (corn) reports 2 decimals but a real TickSize of 0.25, so rounding
    to 2dp (e.g. 494.48) still isn't a valid tick and gets rejected.
    Confirmed live 2026-08-24 on ZC's first real trade. See
    saxo_client.get_tick_size()."""
    if tick_size:
        return round(round(price / tick_size) * tick_size, 10)
    if price_decimals is not None:
        return round(price, price_decimals)
    dp = _PRICE_DP.get(asset_type, 5)
    # JPY crosses quote in 3 decimal places, not 5 (Saxo rejects 5dp for JPY)
    if asset_type == "FxSpot" and symbol.upper().endswith(_JPY_SUFFIX):
        dp = 3
    return round(price, dp)


def _stop_duration(asset_type: str) -> dict:
    return {"DurationType": _DURATION.get(asset_type, "GoodTillCancel")}


def _close_side(buy_sell: str, asset_type: str) -> str:
    """Return the closing side for a stop or limit order."""
    if asset_type in _LONG_ONLY:
        return "Sell"
    return "Sell" if buy_sell == "Buy" else "Buy"


# Asset types that require StopLimit (exchange-listed instruments).
# FxSpot uses plain Stop; everything else needs StopLimit with StopLimitPrice.
_STOP_LIMIT_TYPES: set[str] = {
    "CfdOnIndex", "ContractFutures", "CdfOnEtf", "CfdOnEtf",
    "Etf", "Stock", "CfdOnStock",
}


def _stop_type(buy_sell: str, asset_type: str) -> str:
    """Stop for FxSpot; StopLimit for exchange-listed instruments."""
    return "StopLimit" if asset_type in _STOP_LIMIT_TYPES else "Stop"


def _order_type_not_supported(exc: Exception) -> bool:
    """True if exc is a Saxo HTTPError whose body says the OrderType itself
    isn't accepted for this instrument (as opposed to a price/tick/margin
    rejection, which needs a different fix)."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return False
    try:
        return resp.json().get("ErrorInfo", {}).get("ErrorCode") == "OrderTypeNotSupported"
    except Exception:
        return False


def _post_stop_order(post_fn, stop_body: dict):
    """POST a stop-order body, retrying once with OrderType="StopIfTraded"
    (Saxo's stop-market order type for exchange-listed instruments whose
    native trading rules don't accept plain "Stop"/"StopLimit") if the
    first attempt is rejected with OrderTypeNotSupported.

    Found live 2026-08-24 on ZC (corn)'s first real trade -- the market
    this instrument became tradeable for the same day the capital cap was
    raised (see futures_capital_cap_raised memory). ZC's own
    /ref/v1/instruments/details lists SupportedOrderTypes as
    ['TriggerBreakout', 'TriggerStop', 'TriggerLimit', 'StopIfTraded',
    'TrailingStopIfTraded', 'Limit', 'Market'] -- no 'Stop' or
    'StopLimit' at all, despite ZC sharing AssetType=ContractFutures with
    GC/ES/etc, which DO accept the generic types. This is genuinely
    instrument-specific (CBOT-listed grain futures, likely ZW/ZS/ZB share
    it), not something _stop_type()'s asset-type table can predict up
    front -- caught via a retry-on-rejection instead of a static list, so
    it self-heals for any other instrument with the same quirk. A
    StopIfTraded carries no StopLimitPrice (it's a stop-market type, not
    stop-limit), so that field is dropped on retry."""
    try:
        return post_fn("/trade/v2/orders", stop_body)
    except Exception as exc:
        if not _order_type_not_supported(exc):
            raise
        fallback_body = dict(stop_body)
        fallback_body["OrderType"] = "StopIfTraded"
        fallback_body.pop("StopLimitPrice", None)
        logger.info(
            f"  stop order type not supported for uic {stop_body.get('Uic')}, "
            f"retrying as StopIfTraded"
        )
        return post_fn("/trade/v2/orders", fallback_body)


def _stop_limit_price(order_price: float, close_side: str, asset_type: str,
                      symbol: str = "", price_decimals: int | None = None) -> float:
    """Compute StopLimitPrice: 1% beyond trigger to absorb slippage."""
    if price_decimals is not None:
        dp = price_decimals
    else:
        dp = _PRICE_DP.get(asset_type, 2)
        if asset_type == "FxSpot" and symbol.upper().endswith(_JPY_SUFFIX):
            dp = 3
    if close_side == "Sell":
        return round(order_price * 0.99, dp)
    else:
        return round(order_price * 1.01, dp)


def place_protective_stop(
    post_fn,
    account_key: str,
    uic: int,
    asset_type: str,
    amount: int,
    direction: str,
    stop_price: float,
    label: str = "",
    symbol: str = "",
    price_decimals: int | None = None,
    tick_size: float | None = None,
) -> str | None:
    """Place a standalone protective stop for a position that already
    exists (no entry order). Reuses the exact same per-asset-type rules as
    place_with_stop() (Stop vs StopLimit, price dp, GTC vs DayOrder, close
    side) so a housekeeping/reconciliation fix is held to the same
    tick-size/order-type correctness as a normal entry.

    direction is the POSITION's own side ("Buy"/"Sell"); the stop order's
    BuySell is computed as the closing side automatically.

    tick_size: see _round_price()'s note -- pass the instrument's live
    TickSize (saxo_client.get_tick_size()) for exchange-listed futures
    whose native tick is coarser than its decimal precision suggests.

    Returns the new stop order id, or None if Saxo rejected it.
    """
    close = _close_side(direction, asset_type)
    stype = _stop_type(direction, asset_type)
    dur   = _stop_duration(asset_type)
    rstop = _round_price(stop_price, asset_type, symbol, price_decimals, tick_size)

    body = {
        "AccountKey":    account_key,
        "Uic":           uic,
        "AssetType":     asset_type,
        "Amount":        amount,
        "BuySell":       close,
        "OrderType":     stype,
        "OrderPrice":    rstop,
        "OrderDuration": dur,
        "ManualOrder":   False,
    }
    if stype == "StopLimit":
        body["StopLimitPrice"] = _stop_limit_price(rstop, close, asset_type, symbol, price_decimals)

    try:
        resp = _post_stop_order(post_fn, body)
        oid = str(resp.get("OrderId", "")) or None
        if oid:
            logger.info(f"  [HOUSEKEEPING] Protective stop placed for {label}: "
                        f"{close} {amount} @ {rstop} → {oid}")
        return oid
    except Exception as exc:
        logger.warning(f"  [HOUSEKEEPING] Protective stop FAILED for {label}: {exc}")
        return None


def place_with_stop(
    post_fn,
    account_key: str,
    uic: int,
    asset_type: str,
    amount: int,
    buy_sell: str,
    stop_price: float,
    label: str = "",
    take_profit_price: float | None = None,
    symbol: str = "",
    price_decimals: int | None = None,
    tick_size: float | None = None,
) -> tuple:
    """
    Place a Market entry + native Saxo stop-loss (and optional take-profit).

    Parameters
    ----------
    post_fn           : callable(path, body) → dict
    account_key       : Saxo AccountKey string
    uic               : Saxo UIC integer
    asset_type        : "FxSpot" | "CfdOnIndex" | "ContractFutures" |
                        "CdfOnEtf" | "Etf" | "Stock" | "CfdOnStock"
    amount            : order quantity
    buy_sell          : "Buy" or "Sell"
    stop_price        : stop-loss level
    label             : log label, e.g. "gap:NZDCHF"
    take_profit_price : take-profit level (optional).
                        When provided, a bracket OCO is sent so Saxo
                        cancels the other leg automatically on fill.
    symbol            : FX pair string e.g. "AUDJPY" — used to detect
                        JPY crosses and round to 3dp instead of 5dp,
                        ONLY when price_decimals is not given.
    price_decimals    : the instrument's OWN decimal precision, straight
                        from Saxo's live Format.Decimals (forex.universe's
                        pip_size, already fetched per-pair when the 117-pair
                        universe was built — NOT a guess). Strongly prefer
                        passing this for FxSpot: the JPY-only 5dp/3dp guess
                        below is wrong for any other non-5dp pair — found via
                        a live PriceNotInTickSizeIncrements rejection on
                        AUDTRY (needs 4dp). When this happens the STOP order
                        is rejected outright while the Market ENTRY still
                        goes through (Market orders carry no price to rej-
                        ect), leaving a real position open with no stop.

    Returns
    -------
    (entry_order_id, stop_order_id, tp_order_id)
        tp_order_id is None when no take-profit was requested or it failed.
        stop_order_id is None only if the stop itself failed.
        entry_order_id is None if the ENTRY itself was rejected by Saxo —
        nothing was opened at all. Callers must check for this and skip
        recording a position; do not assume a truthy caller-side value.
    """
    close = _close_side(buy_sell, asset_type)
    stype = _stop_type(buy_sell, asset_type)
    dur   = _stop_duration(asset_type)
    rstop = _round_price(stop_price, asset_type, symbol, price_decimals, tick_size)

    slp = (_stop_limit_price(rstop, close, asset_type, symbol, price_decimals)
           if stype == "StopLimit" else None)

    if take_profit_price is not None:
        rtp = _round_price(take_profit_price, asset_type, symbol, price_decimals, tick_size)
        return _place_bracket(post_fn, account_key, uic, asset_type,
                              amount, buy_sell, close, stype, rstop, rtp, dur, label,
                              stop_limit_price=slp)
    else:
        entry_oid, stop_oid = _place_entry_then_stop(
            post_fn, account_key, uic, asset_type,
            amount, buy_sell, close, stype, rstop, dur, label,
            stop_limit_price=slp)
        return entry_oid, stop_oid, None


def _place_bracket(post_fn, account_key, uic, asset_type,
                   amount, buy_sell, close_side, stop_type,
                   stop_price, tp_price, dur, label, stop_limit_price=None):
    """
    Place entry + stop + take-profit as a single bracket (OCO) order.
    Saxo cancels the surviving leg when the other fires.
    Falls back to separate stop-only if Saxo rejects the bracket.
    """
    stop_leg = {
        "Amount":        amount,
        "AssetType":     asset_type,
        "BuySell":       close_side,
        "OrderType":     stop_type,
        "OrderPrice":    stop_price,
        "OrderDuration": dur,
        "OrderRelation": "IfDoneSlave",
        "ManualOrder":   False,
    }
    if stop_limit_price is not None:
        stop_leg["StopLimitPrice"] = stop_limit_price

    entry_body = {
        "AccountKey":    account_key,
        "Uic":           uic,
        "AssetType":     asset_type,
        "Amount":        amount,
        "BuySell":       buy_sell,
        "OrderType":     "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        "ManualOrder":   False,
        "Orders": [
            stop_leg,
            {
                "Amount":        amount,
                "AssetType":     asset_type,
                "BuySell":       close_side,
                "OrderType":     "Limit",
                "OrderPrice":    tp_price,
                "OrderDuration": dur,
                "OrderRelation": "IfDoneSlave",
                "ManualOrder":   False,
            },
        ],
    }

    try:
        resp      = post_fn("/trade/v2/orders", entry_body)
        entry_oid = resp.get("OrderId", "?")
        # Saxo's response "Orders" array is in the OPPOSITE order from the
        # request's "Orders" array (request sent [stop_leg, tp_leg], but the
        # response's first entry is consistently the take-profit's id, the
        # second the stop's) -- confirmed live 2026-08-24 via a direct
        # request/response comparison (a GBPUSD test bracket: requested
        # [stop@1.89339, tp@1.91339], response child order 0 turned out to
        # be the Limit/tp order, child order 1 the Stop order). The original
        # code trusted request order and stored these SWAPPED into
        # stop_order_id/tp_order_id -- meaning every bracket order ever
        # placed (forex gap fills, ETF entries) had these two labels
        # backwards in local state. Confirmed live impact: a housekeeping
        # cleanup pass, believing it was cancelling an orphaned STOP,
        # actually cancelled the TAKE-PROFIT leg on 7 pending ETF entries
        # (the real stop-loss child survived, undisturbed, since it was
        # never the one referenced by the swapped stop_order_id field).
        child_ids = [o.get("OrderId") for o in resp.get("Orders", [])]
        tp_oid    = str(child_ids[0]) if len(child_ids) > 0 else None
        stop_oid  = str(child_ids[1]) if len(child_ids) > 1 else None
        logger.info(
            f"[bracket] {label}  entry={entry_oid}  "
            f"{stop_type} stop@{stop_price}  Limit tp@{tp_price}  "
            f"stop_id={stop_oid}  tp_id={tp_oid}"
        )
        return str(entry_oid), stop_oid, tp_oid

    except Exception as exc:
        logger.warning(
            f"[bracket] {label}  bracket order rejected ({exc}) — "
            f"falling back to entry + separate stop + separate TP"
        )
        # Fallback: place entry + stop, then a separate GTC Limit for TP
        entry_oid, stop_oid = _place_entry_then_stop(
            post_fn, account_key, uic, asset_type,
            amount, buy_sell, close_side, stop_type, stop_price, dur, label,
            stop_limit_price=stop_limit_price)

        tp_oid = None
        try:
            tp_body = {
                "AccountKey":    account_key,
                "Uic":           uic,
                "AssetType":     asset_type,
                "Amount":        amount,
                "BuySell":       close_side,
                "OrderType":     "Limit",
                "OrderPrice":    tp_price,
                "OrderDuration": dur,
                "ManualOrder":   False,
            }
            tp_resp = post_fn("/trade/v2/orders", tp_body)
            tp_oid  = str(tp_resp.get("OrderId", "?"))
            logger.info(f"[tp] {label}  separate Limit@{tp_price}  tp_id={tp_oid}")
        except Exception as tp_exc:
            logger.warning(f"[tp] {label}  separate TP order FAILED: {tp_exc}")

        return entry_oid, stop_oid, tp_oid


def place_stop_only(post_fn, account_key, uic, asset_type, amount,
                    entry_side, stop_price, symbol: str = "",
                    price_decimals: int | None = None,
                    tick_size: float | None = None) -> str | None:
    """Place a STANDALONE protective stop for an ALREADY-OPEN position --
    no entry order. Used to heal a position whose original bracket/stop was
    rejected (e.g. the 2026-09-01 Saxo SIM "NotOwned" settlement race:
    entry accepted, stop rejected microseconds later). `entry_side` is the
    position's original "Buy"/"Sell"; the stop is placed on the closing
    side. Returns the stop order id, or None on failure (logged, never
    raises).
    """
    close = _close_side(entry_side, asset_type)
    stype = _stop_type(entry_side, asset_type)
    rstop = _round_price(stop_price, asset_type, symbol, price_decimals, tick_size)
    slp = (_stop_limit_price(rstop, close, asset_type, symbol, price_decimals)
           if stype == "StopLimit" else None)
    body = {
        "AccountKey":    account_key,
        "Uic":           uic,
        "AssetType":     asset_type,
        "Amount":        amount,
        "BuySell":       close,
        "OrderType":     stype,
        "OrderPrice":    rstop,
        "OrderDuration": _stop_duration(asset_type),
        "ManualOrder":   False,
    }
    if slp is not None:
        body["StopLimitPrice"] = slp
    try:
        oid = _post_stop_order(post_fn, body).get("OrderId", "?")
        logger.info(f"[stop-heal] {symbol or uic}  {stype}@{rstop}  stop_id={oid}")
        return str(oid)
    except Exception as exc:
        logger.warning(f"[stop-heal] {symbol or uic}  stop FAILED: {exc}")
        return None


def _place_entry_then_stop(post_fn, account_key, uic, asset_type,
                            amount, buy_sell, close_side, stop_type,
                            stop_price, dur, label, stop_limit_price=None):
    """Place entry market order, then a separate stop-loss order.

    Returns (None, None) if the entry order itself is rejected — mirrors
    _place_bracket's graceful degradation. This path (no take-profit) is
    what every strategy except gap/london_breakout uses, so before this
    fix (2026-08-21) an entry rejection here had NO handler at all: it
    raised straight out of place_with_stop, out of _run_entries, and
    crashed the entire scheduled run — every strategy queued after the
    one that hit the rejection silently never got to run that cycle.
    Callers MUST check for a None entry_oid and skip recording a
    position — nothing was actually opened at the broker.
    """
    entry_body = {
        "AccountKey":    account_key,
        "Uic":           uic,
        "AssetType":     asset_type,
        "Amount":        amount,
        "BuySell":       buy_sell,
        "OrderType":     "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        "ManualOrder":   False,
    }
    try:
        entry_resp = post_fn("/trade/v2/orders", entry_body)
        entry_oid  = entry_resp.get("OrderId", "?")
        globals()["LAST_ENTRY_ERROR"] = ""
    except Exception as exc:
        globals()["LAST_ENTRY_ERROR"] = str(exc)[:300]
        logger.error(f"[entry] {label}  entry order REJECTED — no position opened: {exc}")
        return None, None

    stop_body = {
        "AccountKey":    account_key,
        "Uic":           uic,
        "AssetType":     asset_type,
        "Amount":        amount,
        "BuySell":       close_side,
        "OrderType":     stop_type,
        "OrderPrice":    stop_price,
        "OrderDuration": dur,
        "ManualOrder":   False,
    }
    if stop_limit_price is not None:
        stop_body["StopLimitPrice"] = stop_limit_price

    stop_oid = None
    try:
        stop_resp = _post_stop_order(post_fn, stop_body)
        stop_oid  = stop_resp.get("OrderId", "?")
        slp_info  = f"  slp={stop_limit_price}" if stop_limit_price else ""
        logger.info(
            f"[stop] {label}  {stop_type}@{stop_price}{slp_info}  "
            f"dur={dur['DurationType']}  stop_id={stop_oid}"
        )
    except Exception as exc:
        logger.warning(
            f"[stop] {label}  stop order FAILED (entry {entry_oid} still placed): {exc}"
        )

    return str(entry_oid), (str(stop_oid) if stop_oid else None)
