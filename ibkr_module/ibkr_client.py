"""
ibkr_client.py
--------------
Thin wrapper around ib_insync for the IBKR stocks sleeve.

Requires:
    pip install ib_insync
    IB Gateway or TWS running on localhost (paper: port 7497, live: port 7496)
    API access enabled in TWS: Edit → Global Configuration → API → Settings
      ✓ Enable ActiveX and Socket Clients
      Socket port: 7497 (paper) or 7496 (live)
      ✓ Allow connections from localhost only

No Saxo imports. No Avanza imports. Completely standalone.
"""
from __future__ import annotations

import time
from typing import Any

try:
    from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder, util
except ImportError:
    raise ImportError(
        "ib_insync not installed. Run: pip install ib_insync\n"
        "Also ensure IB Gateway or TWS is running on localhost."
    )


# ── Connection ────────────────────────────────────────────────────────────────

def connect(host: str, port: int, client_id: int = 10) -> IB:
    """Connect to IB Gateway / TWS. Returns connected IB instance."""
    ib = IB()
    ib.connect(host, port, clientId=client_id, readonly=False)
    return ib


def disconnect(ib: IB) -> None:
    try:
        ib.disconnect()
    except Exception:
        pass


# ── Account ───────────────────────────────────────────────────────────────────

def _account_tag_map(ib: IB, account_id: str) -> tuple[dict[str, str], str]:
    """Return (tag_map, base_currency).

    IBKR returns balances under two different tag schemes:
      - Standard tags: 'TotalCashValue', 'NetLiquidation', etc.
      - Ledger tags:   '$LEDGER-CashBalance', '$LEDGER-NetLiquidation', etc.
        where currency=='BASE' means "in the account's base currency".
    The base currency code (e.g. 'SEK') is found via the ledger exchange-rate
    row: the non-BASE entry whose rate == 1.0 is the base currency itself.
    """
    values = ib.accountValues(account_id)

    # Find real 3-letter base currency: $LEDGER-ExchangeRate where rate==1.0
    # and currency != "BASE" identifies the base currency itself.
    base = "USD"
    for v in values:
        if v.tag == "$LEDGER-ExchangeRate" and v.currency not in ("BASE", ""):
            try:
                if abs(float(v.value) - 1.0) < 1e-9:
                    base = v.currency
                    break
            except (ValueError, TypeError):
                pass
    # Fallback: explicit BaseCurrency tag
    if base == "USD":
        for v in values:
            if v.tag == "BaseCurrency" and v.value:
                base = v.value
                break

    tag_map: dict[str, str] = {}

    # Collect standard tags denominated in base currency or currency-agnostic
    for v in values:
        if v.currency in (base, ""):
            tag_map[v.tag] = v.value

    # Collect $LEDGER-* rows (currency=="BASE") and alias to standard names.
    # Only fill in if the standard tag is absent or zero.
    ledger_alias = {
        "$LEDGER-CashBalance":    "TotalCashValue",
        "$LEDGER-NetLiquidation": "NetLiquidation",
        "$LEDGER-BuyingPower":    "BuyingPower",
        "$LEDGER-UnrealizedPnL":  "UnrealizedPnL",
        "$LEDGER-RealizedPnL":    "RealizedPnL",
        "$LEDGER-ExcessLiquidity": "ExcessLiquidity",
    }
    for v in values:
        if v.currency == "BASE" and v.tag in ledger_alias:
            alias = ledger_alias[v.tag]
            if not float(tag_map.get(alias) or 0):
                tag_map[alias] = v.value

    return tag_map, base


def get_cash_balance(ib: IB, account_id: str) -> float:
    """Return available cash in the account's base currency."""
    tag_map, _ = _account_tag_map(ib, account_id)
    return float(tag_map.get("TotalCashValue") or 0)


def get_account_summary(ib: IB, account_id: str) -> dict:
    tag_map, base = _account_tag_map(ib, account_id)
    return {
        "net_liquidation": float(tag_map.get("NetLiquidation") or 0),
        "cash_balance":    float(tag_map.get("TotalCashValue") or 0),
        "buying_power":    float(tag_map.get("BuyingPower") or 0),
        "unrealized_pnl":   float(tag_map.get("UnrealizedPnL") or 0),
        "realized_pnl":     float(tag_map.get("RealizedPnL") or 0),
        "excess_liquidity": float(tag_map.get("ExcessLiquidity") or 0),
        "currency":         base,
    }


# ── Positions ─────────────────────────────────────────────────────────────────

def get_positions(ib: IB, account_id: str) -> list[dict]:
    """Return list of open stock positions for this account."""
    result = []
    for pos in ib.positions(account_id):
        if pos.contract.secType != "STK":
            continue
        if pos.position == 0:
            continue
        result.append({
            "symbol":   pos.contract.symbol,
            "qty":      int(pos.position),
            "avg_cost": round(float(pos.avgCost), 4),
            "contract": pos.contract,
        })
    return result


# ── Prices ────────────────────────────────────────────────────────────────────

def get_price(ib: IB, symbol: str, timeout_s: float = 5.0) -> float:
    """Return last traded price for a US stock. Returns 0.0 if unavailable."""
    contract = Stock(symbol, "SMART", "USD")
    try:
        ib.qualifyContracts(contract)
    except Exception:
        return 0.0

    ticker = ib.reqMktData(contract, "", False, False)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ib.sleep(0.25)
        price = ticker.last or ticker.close
        if price and price > 0:
            ib.cancelMktData(contract)
            return float(price)

    ib.cancelMktData(contract)
    # Fall back to delayed close
    delayed = ticker.close
    return float(delayed) if delayed and delayed > 0 else 0.0


def _prices_are_empty(prices: dict[str, float]) -> bool:
    """True if every price is 0, nan, or missing."""
    import math
    return all(not v or math.isnan(v) for v in prices.values())


def get_prices(ib: IB, symbols: list[str]) -> dict[str, float]:
    """Batch price fetch for a list of symbols.

    Tries IBKR real-time → delayed (type 3) → delayed frozen (type 4) in order.
    If all IBKR attempts return 0/nan (common on paper accounts without live
    market-data subscriptions), falls back to Yahoo Finance daily close prices
    via ibkr_signals.yahoo_prices() — uses the 8-hour disk cache when available.
    """
    import math

    if not symbols:
        return {}

    contracts = [Stock(s, "SMART", "USD") for s in symbols]
    try:
        ib.qualifyContracts(*contracts)
    except Exception:
        pass

    def _fetch(market_data_type: int) -> dict[str, float]:
        ib.reqMarketDataType(market_data_type)
        tickers = [ib.reqMktData(c, "", False, False) for c in contracts]
        ib.sleep(3.0)
        out = {}
        for sym, contract, ticker in zip(symbols, contracts, tickers):
            raw = ticker.last or ticker.close
            if raw and not math.isnan(float(raw)) and float(raw) > 0:
                out[sym] = float(raw)
            else:
                out[sym] = 0.0
            ib.cancelMktData(contract)
        return out

    result = _fetch(1)  # real-time
    if _prices_are_empty(result):
        result = _fetch(3)  # delayed 15-min
    if _prices_are_empty(result):
        result = _fetch(4)  # delayed frozen

    # Final fallback: Yahoo Finance daily close (paper account / no subscription)
    if _prices_are_empty(result):
        try:
            from ibkr_module import ibkr_signals as _sig
            yp = _sig.yahoo_prices(symbols)
            if not _prices_are_empty(yp):
                print("  [prices] IBKR market data unavailable — using Yahoo Finance close prices")
                result = {s: yp.get(s, 0.0) for s in symbols}
        except Exception:
            pass

    return result


# ── Orders ────────────────────────────────────────────────────────────────────

def _stock_contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def place_market_order(ib: IB, account_id: str, symbol: str,
                       action: str, qty: int) -> Any:
    """Place a market order. Returns the Trade object."""
    contract = _stock_contract(symbol)
    ib.qualifyContracts(contract)
    order = MarketOrder(action, qty, account=account_id)
    trade = ib.placeOrder(contract, order)
    return trade


def place_stop_order(ib: IB, account_id: str, symbol: str,
                     qty: int, stop_price: float) -> Any:
    """Place a GTC stop-loss sell order. Returns the Trade object."""
    contract = _stock_contract(symbol)
    ib.qualifyContracts(contract)
    order = StopOrder("SELL", qty, round(stop_price, 2), tif="GTC", account=account_id)
    trade = ib.placeOrder(contract, order)
    return trade


def cancel_order(ib: IB, trade: Any) -> None:
    """Cancel an open order by its Trade object."""
    try:
        ib.cancelOrder(trade.order)
    except Exception:
        pass


def get_open_orders(ib: IB) -> list[dict]:
    """Return list of open orders across all contracts."""
    result = []
    for trade in ib.openTrades():
        result.append({
            "order_id": trade.order.orderId,
            "symbol":   trade.contract.symbol,
            "action":   trade.order.action,
            "qty":      trade.order.totalQuantity,
            "status":   trade.orderStatus.status,
        })
    return result


def confirm_fill(ib: IB, trade: Any, timeout_s: int = 90,
                 poll_s: float = 2.0) -> float | None:
    """
    Poll until trade is filled or timeout. Returns fill price or None (timed out).
    Cancels the order on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ib.sleep(poll_s)
        ib.reqOpenOrders()
        status = trade.orderStatus.status
        if status in ("Filled", "Inactive"):
            return float(trade.orderStatus.avgFillPrice or 0)
        if status in ("Cancelled", "ApiCancelled"):
            return None

    # Timeout: cancel
    cancel_order(ib, trade)
    ib.sleep(1.0)
    return None
