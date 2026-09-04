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

def get_cash_balance(ib: IB, account_id: str) -> float:
    """Return available USD cash (TotalCashValue tag)."""
    for v in ib.accountValues(account_id):
        if v.tag == "TotalCashValue" and v.currency == "USD":
            return float(v.value)
    return 0.0


def get_account_summary(ib: IB, account_id: str) -> dict:
    tags = {v.tag: v.value for v in ib.accountValues(account_id) if v.currency in ("USD", "")}
    return {
        "net_liquidation": float(tags.get("NetLiquidation", 0)),
        "cash_balance":    float(tags.get("TotalCashValue", 0)),
        "buying_power":    float(tags.get("BuyingPower", 0)),
        "unrealized_pnl":  float(tags.get("UnrealizedPnL", 0)),
        "realized_pnl":    float(tags.get("RealizedPnL", 0)),
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


def get_prices(ib: IB, symbols: list[str]) -> dict[str, float]:
    """Batch price fetch for a list of symbols."""
    contracts = [Stock(s, "SMART", "USD") for s in symbols]
    try:
        ib.qualifyContracts(*contracts)
    except Exception:
        pass

    tickers = [ib.reqMktData(c, "", False, False) for c in contracts]
    ib.sleep(3.0)

    result = {}
    for sym, contract, ticker in zip(symbols, contracts, tickers):
        price = ticker.last or ticker.close or 0.0
        result[sym] = float(price) if price else 0.0
        ib.cancelMktData(contract)

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
