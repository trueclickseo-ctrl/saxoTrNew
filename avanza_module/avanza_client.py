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
from datetime import date, timedelta
from typing import Any

from avanza import Avanza, OrderType

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


# ── Account discovery ─────────────────────────────────────────────────────────

def get_isk_account_id(client: Avanza) -> str | None:
    """Return the account ID for the ISK account. Falls back to first account."""
    overview = client.get_overview()
    accounts = overview.get("accounts", [])
    for acct in accounts:
        if "ISK" in (acct.get("accountType") or "").upper():
            return str(acct.get("accountId", acct.get("id", "")))
    if accounts:
        return str(accounts[0].get("accountId", accounts[0].get("id", "")))
    return None


def get_account_summary(client: Avanza, account_id: str | None = None) -> dict:
    """Return {account_id, value_sek, buying_power_sek, account_type}."""
    overview = client.get_overview()
    accounts = overview.get("accounts", [])

    target = None
    if account_id:
        for a in accounts:
            aid = str(a.get("accountId", a.get("id", "")))
            if aid == account_id:
                target = a
                break
    if target is None and accounts:
        target = accounts[0]

    if target is None:
        return {"account_id": account_id, "value_sek": 0.0, "buying_power_sek": 0.0}

    return {
        "account_id":      str(target.get("accountId", target.get("id", ""))),
        "account_type":    target.get("accountType", ""),
        "account_name":    target.get("name", ""),
        "value_sek":       float(target.get("ownCapital", target.get("totalValue", 0))),
        "buying_power_sek":float(target.get("buyingPower", 0)),
        "total_profit_sek":float(target.get("totalProfit", 0)),
        "total_profit_pct":float(target.get("totalProfitPercent", 0)),
    }


# ── Positions ─────────────────────────────────────────────────────────────────

def get_positions(client: Avanza, account_id: str | None = None) -> list[dict]:
    """Return open stock positions.

    Each item: {order_book_id, ticker, name, qty, avg_price, current_price,
                value_sek, gain_pct, account_id, currency}
    Filters to account_id if given.
    """
    raw = client.get_accounts_positions()
    result = []

    # The avanza package returns a dict with instrument-type sections.
    # Stocks are under key "withOrderbook" or top-level depending on version.
    sections = []
    if isinstance(raw, dict):
        for key in ("withOrderbook", "stocks", "positions"):
            if key in raw:
                sections = raw[key]
                break
        if not sections and "instrumentPositions" in raw:
            for group in raw.get("instrumentPositions", []):
                sections.extend(group.get("positions", []))
    elif isinstance(raw, list):
        sections = raw

    for item in sections:
        # Support both nested {position:{}, orderbook:{}} and flat format
        if "position" in item and "orderbook" in item:
            pos = item["position"]
            ob  = item["orderbook"]
        else:
            pos = item
            ob  = item.get("orderbook", item)

        acct_id = str(pos.get("accountId", pos.get("account", {}).get("id", "")))
        if account_id and acct_id != account_id:
            continue

        ob_id = str(ob.get("id", ob.get("orderbookId", pos.get("orderbookId", ""))))
        if not ob_id:
            continue

        result.append({
            "order_book_id": ob_id,
            "ticker":        ob.get("tickerSymbol", ob.get("ticker", ob.get("name", ""))),
            "name":          ob.get("name", ""),
            "qty":           int(float(pos.get("volume", pos.get("quantity", 0)))),
            "avg_price":     float(pos.get("averageAcquiredPrice", pos.get("averageCost", 0))),
            "current_price": float(pos.get("lastPrice", pos.get("currentPrice", 0))),
            "value_sek":     float(pos.get("value", pos.get("marketValue", 0))),
            "gain_pct":      float(pos.get("developmentInPercent", pos.get("gainPercent", 0))),
            "account_id":    acct_id,
            "currency":      ob.get("currency", "USD"),
        })

    return result


# ── Instrument price ──────────────────────────────────────────────────────────

def get_stock_price(client: Avanza, order_book_id: str) -> dict:
    """Return {price, currency, name} for a stock. price is 0.0 on failure."""
    try:
        info = client.get_stock_info(order_book_id)
        price = float(
            info.get("lastPrice") or
            info.get("lastPriceSek") or
            info.get("currentPrice") or 0
        )
        return {
            "price":    price,
            "currency": info.get("currency", "USD"),
            "name":     info.get("name", ""),
        }
    except Exception:
        return {"price": 0.0, "currency": "USD", "name": ""}


# ── Orders ────────────────────────────────────────────────────────────────────

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


def search_stocks(client: Avanza, query: str, limit: int = 10) -> list[dict]:
    """Search for stocks. Returns [{id, name, ticker, currency, country}]."""
    try:
        hits = client.search_for_stock(query, limit=limit)
        result = []
        for h in (hits or []):
            result.append({
                "id":       str(h.get("id", h.get("orderbookId", ""))),
                "name":     h.get("name", ""),
                "ticker":   h.get("tickerSymbol", h.get("ticker", "")),
                "currency": h.get("currency", ""),
                "country":  h.get("flagCode", h.get("country", "")),
                "market":   h.get("marketList", h.get("market", "")),
            })
        return result
    except Exception:
        return []
