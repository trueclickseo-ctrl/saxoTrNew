"""
binance_bot/binance_client.py
------------------------------
Low-level Binance testnet client.

Auth model: HMAC-SHA256 (API key + secret), NOT OAuth2.
            Every signed request adds a timestamp and a signature over the
            query string/body.  The 'python-binance' library handles this
            automatically when testnet=True.

Testnet base URLs (hardcoded â€” no mainnet path exists here by design):
  Spot REST : https://testnet.binance.vision/api
  Spot WS   : wss://testnet.binance.vision/ws
  Futures   : https://testnet.binancefuture.com/fapi  (only if extended)

This file is the only place that touches python-binance directly.
Strategy code must go through BinanceAdapter, not this class.

Package note: this module lives in binance_bot/ (not binance/) to avoid a
namespace clash with the installed python-binance package, which also registers
itself as 'binance' in site-packages.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Optional

from binance_bot.logger import get_logger

log = get_logger(__name__)

# python-binance is an optional dependency at import time so the rest of
# the codebase can import without it being installed.
try:
    from binance.client import Client as _BinanceSDK         # type: ignore
    from binance import exceptions as _bn_exc                # type: ignore
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    _BinanceSDK    = None
    _bn_exc        = None


TESTNET_REST_BASE = "https://testnet.binance.vision/api"
TESTNET_WS_BASE   = "wss://testnet.binance.vision/ws"


class BinanceClient:
    """
    Thin wrapper around python-binance SDK, locked to testnet.

    Instantiate via BinanceClient.from_env() to load credentials from
    the .env.binance file loaded by the bot entry point.
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        if not _SDK_AVAILABLE:
            raise RuntimeError(
                "python-binance is not installed. "
                "Run: pip install python-binance"
            )
        self._client = _BinanceSDK(
            api_key=api_key,
            api_secret=api_secret,
            testnet=True,
        )
        # Override the SDK's tld/base URL to always point at testnet
        self._client.API_URL = TESTNET_REST_BASE
        log.info("BinanceClient connected to TESTNET (%s)", TESTNET_REST_BASE)

    # â”€â”€ Constructors â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @classmethod
    def from_env(cls) -> "BinanceClient":
        """Read credentials from environment variables set by .env.binance."""
        key    = os.environ.get("BINANCE_TESTNET_API_KEY", "")
        secret = os.environ.get("BINANCE_TESTNET_API_SECRET", "")
        if not key or not secret:
            raise ValueError(
                "BINANCE_TESTNET_API_KEY / BINANCE_TESTNET_API_SECRET not set. "
                "Copy .env.binance, fill in your testnet keys, and load it before "
                "running the bot (e.g. via python-dotenv in run_binance_bot.py)."
            )
        return cls(api_key=key, api_secret=secret)

    # â”€â”€ Market data â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def get_symbol_ticker(self, symbol: str) -> dict:
        """Return {symbol, price} for symbol, e.g. 'BTCUSDT'."""
        return self._client.get_symbol_ticker(symbol=symbol)

    def get_order_book(self, symbol: str, limit: int = 5) -> dict:
        """Return top-N bids/asks."""
        return self._client.get_order_book(symbol=symbol, limit=limit)

    def get_klines(self, symbol: str, interval: str, limit: int = 100) -> list:
        """
        Return OHLCV klines.
        interval: "1m", "5m", "15m", "1h", "4h", "1d" etc.
        Each item: [open_time, open, high, low, close, volume, ...]
        """
        return self._client.get_klines(
            symbol=symbol, interval=interval, limit=limit
        )

    def get_ticker_24h(self, symbol: str) -> dict:
        """Return 24-hour rolling stats including volume."""
        return self._client.get_ticker(symbol=symbol)

    # â”€â”€ Account â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def get_account(self) -> dict:
        """Return full account info including balances."""
        return self._client.get_account()

    def get_asset_balance(self, asset: str) -> dict:
        """Return {asset, free, locked} for a single asset (e.g. 'USDT')."""
        return self._client.get_asset_balance(asset=asset)

    def get_open_orders(self, symbol: Optional[str] = None) -> list:
        """Return all open orders, optionally filtered by symbol."""
        kwargs = {"symbol": symbol} if symbol else {}
        return self._client.get_open_orders(**kwargs)

    def get_all_orders(self, symbol: str, limit: int = 50) -> list:
        """Return recent order history for a symbol."""
        return self._client.get_all_orders(symbol=symbol, limit=limit)

    def get_my_trades(self, symbol: str, limit: int = 50) -> list:
        """Return recent trade fills for a symbol."""
        return self._client.get_my_trades(symbol=symbol, limit=limit)

    # â”€â”€ Orders â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def place_market_order(self, symbol: str, side: str, quantity: str) -> dict:
        """
        Place a market order.
        side:     'BUY' | 'SELL'
        quantity: string formatted to the symbol's lot-size precision
        """
        log.info("Market %s %s qty=%s (testnet)", side, symbol, quantity)
        return self._client.order_market(
            symbol=symbol,
            side=side,
            quantity=quantity,
        )

    def place_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: str,
        price: str,
        time_in_force: str = "GTC",
    ) -> dict:
        """Place a limit order at the specified price."""
        log.info(
            "Limit %s %s qty=%s price=%s (testnet)", side, symbol, quantity, price
        )
        return self._client.order_limit(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            timeInForce=time_in_force,
        )

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        """Cancel an open order by orderId."""
        log.info("Cancel order %s for %s (testnet)", order_id, symbol)
        return self._client.cancel_order(symbol=symbol, orderId=order_id)

    def cancel_all_orders(self, symbol: str) -> list:
        """Cancel all open orders for a symbol."""
        log.info("Cancel ALL open orders for %s (testnet)", symbol)
        return self._client.cancel_open_orders(symbol=symbol)

    # â”€â”€ Exchange metadata â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    def get_symbol_info(self, symbol: str) -> Optional[dict]:
        """Return exchange info filters for symbol (lot size, min notional, etc.)."""
        return self._client.get_symbol_info(symbol)

    def get_lot_precision(self, symbol: str) -> int:
        """Return the number of decimal places allowed for quantity."""
        info = self.get_symbol_info(symbol)
        if not info:
            return 8
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step = f["stepSize"].rstrip("0")
                if "." in step:
                    return len(step.split(".")[1])
                return 0
        return 8

    def format_qty(self, symbol: str, qty: float) -> str:
        """Format a quantity to the exchange's required precision."""
        precision = self.get_lot_precision(symbol)
        return f"{qty:.{precision}f}"
