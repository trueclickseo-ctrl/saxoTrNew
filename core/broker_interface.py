"""
core/broker_interface.py
------------------------
Abstract broker interface that every broker adapter must implement.

Both the Saxo adapter and the Binance adapter implement this contract,
which lets strategy code stay broker-agnostic.

Symbol convention differs per broker:
  Saxo:    instrument UIC integers  (e.g. 211)        -- adapter translates
  Binance: base/quote pair strings  (e.g. "BTCUSDT")  -- adapter passes through

Strategy code works with whatever string the adapter normalises to; it never
calls the low-level client directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


# ── Shared data classes (broker-agnostic) ─────────────────────────────────────

@dataclass
class MarketData:
    symbol: str
    price: Decimal
    bid: Optional[Decimal]
    ask: Optional[Decimal]
    volume_24h: Optional[Decimal]   # base-currency volume over trailing 24 h


@dataclass
class Position:
    symbol: str
    qty: Decimal           # positive = long, negative = short
    avg_entry_price: Decimal
    unrealised_pnl: Optional[Decimal] = None
    strategy_tag: str = ""


@dataclass
class OrderResult:
    order_id: str
    symbol: str
    side: str              # "BUY" | "SELL"
    qty: Decimal
    filled_price: Optional[Decimal]
    status: str            # "FILLED" | "NEW" | "PARTIALLY_FILLED" | "CANCELLED" | "REJECTED"
    raw: dict | None = None   # broker-native response, for debugging


@dataclass
class AccountBalance:
    total_equity: Decimal       # total account value in quote currency
    available_cash: Decimal     # cash free to deploy
    currency: str               # e.g. "USDT", "SEK", "USD"


# ── Abstract interface ────────────────────────────────────────────────────────

class BrokerInterface(ABC):
    """
    Abstract base class every broker adapter must implement.

    All monetary amounts are Decimal to avoid float rounding errors.
    All methods that make network calls may raise BrokerError.
    """

    # ── Market data ───────────────────────────────────────────────────────────

    @abstractmethod
    def get_market_data(self, symbol: str) -> MarketData:
        """Return current price, bid/ask, and 24-hour volume for symbol."""

    @abstractmethod
    def get_ohlcv(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> list[dict]:
        """
        Return OHLCV candles as a list of dicts with keys:
          open_time, open, high, low, close, volume
        interval follows Binance convention: "1m", "5m", "1h", "1d", etc.
        Adapters for other brokers translate as needed.
        """

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,           # "BUY" | "SELL"
        qty: Decimal,
        order_type: str,     # "MARKET" | "LIMIT"
        price: Optional[Decimal] = None,
        strategy_tag: str = "",
    ) -> OrderResult:
        """Place an order. Returns OrderResult with fill details."""

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str = "") -> bool:
        """Cancel an open order. Returns True if successfully cancelled."""

    # ── Positions & balance ───────────────────────────────────────────────────

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Return all open positions held in this account."""

    @abstractmethod
    def get_account_balance(self) -> AccountBalance:
        """Return equity, free cash, and account currency."""


# ── Exception ─────────────────────────────────────────────────────────────────

class BrokerError(Exception):
    """Raised when a broker API call fails or returns an unexpected response."""
