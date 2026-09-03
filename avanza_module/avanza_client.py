"""
avanza_client.py
----------------
Thin wrapper around the `avanza` Python package (pip install avanza).

Credentials come ONLY from environment variables — never hardcoded:
    AVANZA_USERNAME    - Avanza login username or personal number
    AVANZA_PASSWORD    - Avanza login password
    AVANZA_TOTP_SECRET - Base-32 TOTP secret (from Google Authenticator setup)

Never import Saxo modules here. This file is the only place that touches the
avanza package directly; all other avanza_module files go through this one.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta
from typing import Any

from avanza import Avanza, OrderType
from avanza.entities import StopLossOrderEvent, StopLossTrigger
from avanza.constants import StopLossPriceType, StopLossTriggerType

_REQUIRED = ("AVANZA_USERNAME", "AVANZA_PASSWORD", "AVANZA_TOTP_SECRET")


def get_client() -> Avanza:
    """Return an authenticated Avanza client. Raises EnvironmentError if creds missing."""
    missing = [v for v in _REQUIRED if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(
            f"Missing env vars: {', '.join(missing)}\n"
            f"Set them in .env.avanza (see .env.avanza.example) and load it before running."
        )
    return Avanza({
        "username":   os.environ["AVANZA_USERNAME"],
        "password":   os.environ["AVANZA_PASSWORD"],
        "totpSecret": os.environ["AVANZA_TOTP_SECRET"],
    })


# ── API response helpers ──────────────────────────────────────────────────────

def _mv(field, default: float = 0.0) -> float:
    """Extract a monetary value from an Avanza API nested {value, unit} dict.

    The Avanza API wraps every numeric in {"value": x, "unit": "SEK", ...}.
    Handles the case where the field is already a plain number (old API compat).
    """
    if isinstance(field, dict):
        return float(field.get("value", default))
    if field is None:
        return default
    try:
        return float(field)
    except (TypeError, ValueError):
        return default


def _account_name(raw_name) -> str:
    """Extract display name from Avanza's nested name field."""
    if isinstance(raw_name, dict):
        return raw_name.get("userDefinedName") or raw_name.get("defaultName") or ""
    return str(raw_name) if raw_name else ""


# Avanza uses INVESTERINGSSPARKONTO (Swedish) for what marketing calls ISK.
_ISK_TYPE = "INVESTERINGSSPARKONTO"


# ── Account discovery ─────────────────────────────────────────────────────────

def get_isk_account_id(client: Avanza) -> str | None:
    """Return the account ID for the first ISK (INVESTERINGSSPARKONTO) account.
    Falls back to the first account if none is tagged ISK.
    """
    overview = client.get_overview()
    accounts = overview.get("accounts", [])
    for acct in accounts:
        if acct.get("type", "") == _ISK_TYPE:
            return str(acct.get("id", ""))
    if accounts:
        return str(accounts[0].get("id", ""))
    return None


def get_account_summary(client: Avanza, account_id: str | None = None) -> dict:
    """Return {account_id, value_sek, buying_power_sek, account_type, account_name}."""
    overview = client.get_overview()
    accounts = overview.get("accounts", [])

    target = None
    if account_id:
        for a in accounts:
            if str(a.get("id", "")) == account_id:
                target = a
                break
    if target is None and accounts:
        target = accounts[0]

    if target is None:
        return {"account_id": account_id, "value_sek": 0.0, "buying_power_sek": 0.0}

    perf_all = (target.get("performance") or {}).get("ALL_TIME", {})
    total_profit_pct = _mv(perf_all.get("relative"), 0.0)

    return {
        "account_id":      str(target.get("id", "")),
        "account_type":    target.get("type", ""),
        "account_name":    _account_name(target.get("name", "")),
        "value_sek":       _mv(target.get("totalValue")),
        "buying_power_sek":_mv(target.get("buyingPower")),
        "total_profit_sek":_mv((target.get("profit") or {}).get("absolute")),
        "total_profit_pct":total_profit_pct,
    }


# ── Positions ─────────────────────────────────────────────────────────────────

def get_positions(client: Avanza, account_id: str | None = None) -> list[dict]:
    """Return open positions from all Avanza accounts.

    Each item: {order_book_id, ticker, name, qty, avg_price, current_price,
                value_sek, gain_pct, account_id, currency}
    Filters to account_id if given.

    The Avanza API returns positions under "withOrderbook" as a list of:
      {account: {id, type, name}, instrument: {orderbook: {id, name, quote},
       currency}, volume: {value}, value: {value}, averageAcquiredPrice: {value},
       acquiredValue: {value}}
    """
    raw = client.get_accounts_positions()
    result = []

    sections = []
    if isinstance(raw, dict):
        sections = raw.get("withOrderbook") or []
    elif isinstance(raw, list):
        sections = raw

    for item in sections:
        acct    = item.get("account") or {}
        instr   = item.get("instrument") or {}
        ob      = instr.get("orderbook") or {}
        quote   = ob.get("quote") or {}

        acct_id = str(acct.get("id", ""))
        if account_id and acct_id != account_id:
            continue

        ob_id = str(ob.get("id", ""))
        if not ob_id:
            continue

        qty          = _mv(item.get("volume"), 0.0)
        avg_price    = _mv(item.get("averageAcquiredPrice"))
        cur_price    = _mv((quote.get("latest") or quote.get("highest")))
        value_sek    = _mv(item.get("value"))
        acquired_sek = _mv(item.get("acquiredValue"))
        gain_pct     = ((value_sek - acquired_sek) / acquired_sek * 100
                        if acquired_sek else 0.0)

        # tickerSymbol exists on stocks; funds only have a name
        ticker = ob.get("tickerSymbol") or ob.get("shortName") or ob.get("name", "")

        result.append({
            "order_book_id": ob_id,
            "ticker":        ticker,
            "name":          instr.get("name") or ob.get("name", ""),
            "qty":           qty,
            "avg_price":     avg_price,
            "current_price": cur_price,
            "value_sek":     value_sek,
            "gain_pct":      round(gain_pct, 2),
            "account_id":    acct_id,
            "currency":      instr.get("currency", "USD"),
        })

    return result


# ── Instrument price ──────────────────────────────────────────────────────────

def get_stock_price(client: Avanza, order_book_id: str) -> dict:
    """Return {price, currency, name} for a stock. price is 0.0 on failure.

    Avanza quote uses 'last' (plain float) for the last traded price.
    Currency lives in listing.currency or listing.tickerSymbol context;
    US stocks default to USD.
    """
    try:
        info    = client.get_stock_info(order_book_id)
        quote   = info.get("quote") or {}
        listing = info.get("listing") or {}
        price   = float(quote.get("last") or quote.get("buy") or 0.0)
        currency = listing.get("currency", info.get("currency", "USD"))
        return {
            "price":    price,
            "currency": currency,
            "name":     info.get("name", ""),
        }
    except Exception:
        return {"price": 0.0, "currency": "USD", "name": ""}


# ── Orders ────────────────────────────────────────────────────────────────────

def cancel_order(client: Avanza, account_id: str, order_id: str) -> bool:
    """Cancel an open limit order. Returns True on success."""
    try:
        client.delete_order(account_id=account_id, order_id=order_id)
        return True
    except Exception as exc:
        print(f"  [avanza] cancel_order {order_id}: {exc}", file=sys.stderr)
        return False


def confirm_fill(client: Avanza, account_id: str, order_id: str, ob_id: str,
                 timeout_s: int = 120, poll_s: int = 10) -> float | None:
    """Poll until a limit order fills, then return the actual fill price.

    Polls open orders every `poll_s` seconds. When the order disappears from
    open orders, reads the position's averageAcquiredPrice as the fill price.

    If the order is still pending after `timeout_s` seconds, it is cancelled
    and None is returned.

    Returns: fill_price (float) on success, None if cancelled/timeout.
    """
    deadline = time.monotonic() + timeout_s
    print(f"    Polling fill (order={order_id}, up to {timeout_s}s)...", end="", flush=True)

    while time.monotonic() < deadline:
        time.sleep(poll_s)
        print(".", end="", flush=True)

        try:
            open_orders = get_open_orders(client)
        except Exception:
            continue

        still_open = any(str(o.get("order_id", "")) == str(order_id) for o in open_orders)
        if not still_open:
            print(" filled")
            # Read fill price from the position that just opened
            try:
                positions = get_positions(client, account_id)
                for pos in positions:
                    if str(pos.get("order_book_id", "")) == str(ob_id):
                        fill_price = pos.get("avg_price") or pos.get("current_price") or 0.0
                        if fill_price:
                            return float(fill_price)
            except Exception:
                pass
            # Fallback: we know it filled but can't read price; return 0 signals caller to
            # use the limit price as a proxy
            return 0.0

    # Timeout — cancel
    print(" timeout → cancelling")
    cancel_order(client, account_id, order_id)
    return None


def get_open_orders(client: Avanza) -> list[dict]:
    """Return currently open orders."""
    try:
        raw = client.get_orders()
        orders = []
        if isinstance(raw, dict):
            orders = raw.get("orders", raw.get("data", []))
        elif isinstance(raw, list):
            orders = raw
        result = []
        for o in orders:
            ob = o.get("orderbook", o.get("instrument", {}))
            result.append({
                "order_id":      str(o.get("orderId", o.get("id", ""))),
                "order_book_id": str(ob.get("id", o.get("orderbookId", ""))),
                "ticker":        ob.get("tickerSymbol", ob.get("name", "")),
                "side":          o.get("orderType", o.get("side", "")),
                "qty":           int(float(o.get("volume", o.get("quantity", 0)))),
                "price":         float(o.get("price", 0)),
            })
        return result
    except Exception:
        return []


def place_buy(client: Avanza, account_id: str, order_book_id: str,
              qty: int, price: float) -> dict:
    """Place a BUY limit order valid for 7 days. Returns Avanza response dict."""
    return client.place_order(
        account_id=account_id,
        order_book_id=order_book_id,
        order_type=OrderType.BUY,
        price=round(price, 2),
        valid_until=date.today() + timedelta(days=7),
        volume=qty,
    )


def place_sell(client: Avanza, account_id: str, order_book_id: str,
               qty: int, price: float) -> dict:
    """Place a SELL limit order valid for 7 days. Returns Avanza response dict."""
    return client.place_order(
        account_id=account_id,
        order_book_id=order_book_id,
        order_type=OrderType.SELL,
        price=round(price, 2),
        valid_until=date.today() + timedelta(days=7),
        volume=qty,
    )


# ── Stop-loss orders ──────────────────────────────────────────────────────────

def place_stop_loss(client: Avanza, account_id: str, order_book_id: str,
                    qty: int, stop_price: float,
                    sell_slippage_pct: float = 0.01) -> dict:
    """Place a LESS_OR_EQUAL stop-loss that sells qty shares when price hits stop_price.

    The sell order is a limit at stop_price × (1 - sell_slippage_pct) to
    ensure fill through normal bid/ask spread. Valid 365 days (GTC equivalent).

    Returns Avanza response: {status: 'SUCCESS', stoplossOrderId: str} or error dict.
    """
    trigger = StopLossTrigger(
        type=StopLossTriggerType.LESS_OR_EQUAL,
        value=round(stop_price, 2),
        valid_until=date.today() + timedelta(days=365),
        value_type=StopLossPriceType.MONETARY,
        trigger_on_market_maker_quote=False,
    )
    sell_price = round(stop_price * (1 - sell_slippage_pct), 2)
    event = StopLossOrderEvent(
        type=OrderType.SELL,
        price=sell_price,
        volume=qty,
        valid_days=1,
        price_type=StopLossPriceType.MONETARY,
        short_selling_allowed=False,
    )
    return client.place_stop_loss_order(
        parent_stop_loss_id="0",
        account_id=account_id,
        order_book_id=order_book_id,
        stop_loss_trigger=trigger,
        stop_loss_order_event=event,
    )


def delete_stop_loss(client: Avanza, account_id: str, stop_loss_id: str) -> bool:
    """Cancel an existing stop-loss order. Returns True on success."""
    try:
        client.delete_stop_loss_order(account_id=account_id, stop_loss_id=stop_loss_id)
        return True
    except Exception as exc:
        print(f"  [avanza] delete_stop_loss {stop_loss_id}: {exc}", file=sys.stderr)
        return False


def get_stop_losses(client: Avanza) -> list[dict]:
    """Return all open Avanza stop-loss orders.

    Each item: {stop_loss_id, order_book_id, ticker, account_id,
                trigger_price, volume, status, deletable}
    """
    try:
        raw = client.get_all_stop_losses()
        if not isinstance(raw, list):
            return []
        result = []
        for sl in raw:
            trigger = sl.get("trigger", {})
            order   = sl.get("order", {})
            ob      = sl.get("orderbook", {})
            acct    = sl.get("account", {})
            result.append({
                "stop_loss_id":  str(sl.get("id", "")),
                "order_book_id": str(ob.get("id", "")),
                "ticker":        ob.get("shortName", ob.get("name", "")),
                "account_id":    str(acct.get("id", "")),
                "trigger_price": float(trigger.get("value", 0)),
                "volume":        int(order.get("volume", 0)),
                "status":        sl.get("status", ""),
                "deletable":     bool(sl.get("deletable", False)),
            })
        return result
    except Exception as exc:
        print(f"  [avanza] get_stop_losses: {exc}", file=sys.stderr)
        return []


def _extract_ticker_from_title(title: str) -> str:
    """Extract ticker symbol from Avanza title like 'Dell Technologies C (DELL)'.
    Returns the text inside the last parentheses pair, or '' if none.
    """
    if title and "(" in title and title.endswith(")"):
        return title.rsplit("(", 1)[-1].rstrip(")")
    return ""


def search_stocks(client: Avanza, query: str, limit: int = 10) -> list[dict]:
    """Search for stocks. Returns [{id, name, ticker, currency, country, market}].

    Avanza search results use 'orderBookId' for the instrument ID and embed
    the ticker inside 'title' as 'Name (TICKER)'. price fields use Swedish
    comma-decimal strings — we don't parse them here.
    """
    try:
        hits = client.search_for_stock(query, limit=limit)
        result = []
        for h in (hits or []):
            title  = h.get("title", "")
            ticker = _extract_ticker_from_title(title) or h.get("ticker", "")
            price  = h.get("price") or {}
            result.append({
                "id":       str(h.get("orderBookId", h.get("orderbookId", h.get("id", "")))),
                "name":     title.rsplit("(", 1)[0].strip() if "(" in title else title,
                "ticker":   ticker,
                "currency": price.get("currency", h.get("currency", "")),
                "country":  h.get("flagCode", h.get("country", "")),
                "market":   h.get("marketPlaceName", h.get("marketList", h.get("market", ""))),
            })
        return result
    except Exception:
        return []
