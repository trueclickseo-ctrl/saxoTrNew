"""
binance/binance_adapter.py
---------------------------
Implements core/broker_interface.BrokerInterface for Binance testnet.

This is the only file strategy code should import from — it hides
binance_client.py behind the standard BrokerInterface contract so strategies
stay broker-agnostic.

All amounts are Decimal to avoid float drift. Quantities are formatted to
the exchange's lot-size precision before any order is sent.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from core.broker_interface import (
    AccountBalance,
    BrokerError,
    BrokerInterface,
    MarketData,
    OrderResult,
    Position,
)
from binance.binance_client import BinanceClient
from binance.logger import get_logger

log = get_logger(__name__)


class BinanceAdapter(BrokerInterface):
    """
    Binance testnet adapter.

    Instantiate with a BinanceClient, then pass this adapter to strategies.

    Example:
        client  = BinanceClient.from_env()
        adapter = BinanceAdapter(client)
        data    = adapter.get_market_data("BTCUSDT")
    """

    def __init__(self, client: BinanceClient) -> None:
        self._c = client

    # ── Market data ───────────────────────────────────────────────────────────

    def get_market_data(self, symbol: str) -> MarketData:
        try:
            ticker = self._c.get_ticker_24h(symbol)
            book   = self._c.get_order_book(symbol, limit=1)
            bid    = Decimal(book["bids"][0][0]) if book.get("bids") else None
            ask    = Decimal(book["asks"][0][0]) if book.get("asks") else None
            return MarketData(
                symbol=symbol,
                price=Decimal(ticker["lastPrice"]),
                bid=bid,
                ask=ask,
                volume_24h=Decimal(ticker["volume"]),
            )
        except Exception as exc:
            raise BrokerError(f"get_market_data({symbol}) failed: {exc}") from exc

    def get_ohlcv(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> list[dict]:
        """
        Return OHLCV bars as list of dicts.
        Converts raw Binance kline lists into named dicts for readability.
        """
        try:
            raw = self._c.get_klines(symbol=symbol, interval=interval, limit=limit)
            return [
                {
                    "open_time": int(k[0]),
                    "open":      Decimal(k[1]),
                    "high":      Decimal(k[2]),
                    "low":       Decimal(k[3]),
                    "close":     Decimal(k[4]),
                    "volume":    Decimal(k[5]),
                }
                for k in raw
            ]
        except Exception as exc:
            raise BrokerError(f"get_ohlcv({symbol}, {interval}) failed: {exc}") from exc

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: Decimal,
        order_type: str,
        price: Optional[Decimal] = None,
        strategy_tag: str = "",
    ) -> OrderResult:
        try:
            qty_str = self._c.format_qty(symbol, float(qty))

            if order_type == "MARKET":
                raw = self._c.place_market_order(symbol=symbol, side=side, quantity=qty_str)
            elif order_type == "LIMIT":
                if price is None:
                    raise ValueError("price required for LIMIT orders")
                info  = self._c.get_symbol_info(symbol)
                p_prec = self._price_precision(info)
                price_str = f"{float(price):.{p_prec}f}"
                raw = self._c.place_limit_order(
                    symbol=symbol, side=side, quantity=qty_str, price=price_str
                )
            else:
                raise ValueError(f"Unsupported order_type: {order_type}")

            filled_price = None
            if raw.get("fills"):
                # Average fill price across partial fills
                total_qty   = sum(Decimal(f["qty"])   for f in raw["fills"])
                total_notl  = sum(Decimal(f["qty"]) * Decimal(f["price"]) for f in raw["fills"])
                filled_price = total_notl / total_qty if total_qty else None

            log.info(
                "[%s] %s %s %s qty=%s fill=%s",
                strategy_tag or "?", order_type, side, symbol, qty_str, filled_price
            )
            return OrderResult(
                order_id=str(raw.get("orderId", "")),
                symbol=symbol,
                side=side,
                qty=Decimal(qty_str),
                filled_price=filled_price,
                status=raw.get("status", "UNKNOWN"),
                raw=raw,
            )
        except BrokerError:
            raise
        except Exception as exc:
            raise BrokerError(f"place_order({symbol}, {side}, {qty}) failed: {exc}") from exc

    def cancel_order(self, order_id: str, symbol: str = "") -> bool:
        try:
            self._c.cancel_order(symbol=symbol, order_id=int(order_id))
            return True
        except Exception as exc:
            log.warning("cancel_order(%s, %s) failed: %s", order_id, symbol, exc)
            return False

    # ── Positions & balance ───────────────────────────────────────────────────

    def get_positions(self) -> list[Position]:
        """
        For spot accounts: return non-zero balances as positions.
        Quote asset (USDT) is excluded — only base assets are listed.
        """
        try:
            info = self._c.get_account()
            positions = []
            for b in info.get("balances", []):
                free   = Decimal(b["free"])
                locked = Decimal(b["locked"])
                total  = free + locked
                if total > 0 and b["asset"] != "USDT":
                    ticker = self._c.get_symbol_ticker(b["asset"] + "USDT")
                    price  = Decimal(ticker["price"])
                    positions.append(
                        Position(
                            symbol=b["asset"] + "USDT",
                            qty=total,
                            avg_entry_price=price,  # spot has no stored avg cost — use mid
                            unrealised_pnl=None,
                        )
                    )
            return positions
        except Exception as exc:
            raise BrokerError(f"get_positions() failed: {exc}") from exc

    def get_account_balance(self) -> AccountBalance:
        """Return free USDT balance as available cash."""
        try:
            bal = self._c.get_asset_balance("USDT")
            free   = Decimal(bal["free"])
            locked = Decimal(bal["locked"])
            return AccountBalance(
                total_equity=free + locked,
                available_cash=free,
                currency="USDT",
            )
        except Exception as exc:
            raise BrokerError(f"get_account_balance() failed: {exc}") from exc

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _price_precision(symbol_info: Optional[dict]) -> int:
        if not symbol_info:
            return 2
        for f in symbol_info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                step = f["tickSize"].rstrip("0")
                if "." in step:
                    return len(step.split(".")[1])
                return 0
        return 2
