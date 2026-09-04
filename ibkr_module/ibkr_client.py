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
    import logging
    # Suppress noisy ib_insync warnings before connecting (completed orders timeout,
    # market-data errors 10089/10168/300, etc.) — expected on paper accounts.
    for _log in ("ib_insync", "ib_insync.ib", "ib_insync.wrapper",
                 "ib_insync.client", "ib_insync.ticker"):
        logging.getLogger(_log).setLevel(logging.CRITICAL)
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

    Error 10168 (no subscription) and Error 300 (cancel-before-subscribe race)
    are suppressed silently — they are expected on paper accounts and the Yahoo
    fallback handles the missing prices.
    """
    import math

    if not symbols:
        return {}

    # Suppress noisy but harmless paper-account market-data errors.
    _SUPPRESS = {10089, 10168, 10182, 300, 354}

    def _err_suppress(reqId, errorCode, errorString, contract):
        if errorCode not in _SUPPRESS:
            print(f"Error {errorCode}, reqId {reqId}: {errorString}")

    ib.errorEvent += _err_suppress

    try:
        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        try:
            ib.qualifyContracts(*contracts)
        except Exception:
            pass

        _BATCH = 50  # IBKR paper accounts cap concurrent ticker subscriptions

        def _fetch(market_data_type: int) -> dict[str, float]:
            ib.reqMarketDataType(market_data_type)
            out = {}
            pairs = list(zip(symbols, contracts))
            for i in range(0, len(pairs), _BATCH):
                chunk = pairs[i:i + _BATCH]
                chunk_tickers = [ib.reqMktData(c, "", False, False) for _, c in chunk]
                ib.sleep(3.0)
                for (sym, contract), ticker in zip(chunk, chunk_tickers):
                    raw = ticker.last or ticker.close
                    if raw and not math.isnan(float(raw)) and float(raw) > 0:
                        out[sym] = float(raw)
                    else:
                        out[sym] = 0.0
                    try:
                        ib.cancelMktData(contract)
                    except Exception:
                        pass
            return out

        result = _fetch(1)  # real-time
        if _prices_are_empty(result):
            result = _fetch(3)  # delayed 15-min
        if _prices_are_empty(result):
            result = _fetch(4)  # delayed frozen

        if _prices_are_empty(result):
            print("  [prices] IBKR returned no market data — using Yahoo fallback.")

        return result

    finally:
        try:
            ib.errorEvent -= _err_suppress
        except Exception:
            pass


def abs_price(price: float) -> float:
    """Return the price as-is (compatibility shim — Yahoo fallback removed)."""
    return float(price) if price else 0.0


def is_market_open() -> bool:
    """Return True if the US stock market is currently open (09:30–16:00 ET)."""
    import datetime as _dt
    try:
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
    except ImportError:
        import datetime
        # UTC-4 (EDT) / UTC-5 (EST) — approximate with fixed -4 offset
        et = _dt.timezone(_dt.timedelta(hours=-4))
    now_et = _dt.datetime.now(tz=et)
    if now_et.weekday() >= 5:   # Saturday / Sunday
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close


# ── Orders ────────────────────────────────────────────────────────────────────

def _stock_contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def place_market_order(ib: IB, account_id: str, symbol: str,
                       action: str, qty: int) -> Any:
    """Place a market order. Returns the Trade object."""
    contract = _stock_contract(symbol)
    ib.qualifyContracts(contract)
    order = MarketOrder(action, qty, tif="DAY", account=account_id)
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
