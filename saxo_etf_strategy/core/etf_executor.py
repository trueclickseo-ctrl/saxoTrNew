"""
Order execution for the ETF strategy.

Uses its own capital allocation (config.risk.*) — entirely separate from the
shares strategies' capital and risk budget. Set dry_run=True (the default)
to log intended orders without sending them to Saxo.

Key fixes vs. the original scaffold:
  - ManualOrder: False added to all orders (required by Saxo since ~2024)
  - AccountKey auto-discovered if not set in config
  - entry_price stored in position state so review_exits() can compute P&L
  - review_exits() fully implemented: live price via /trade/v1/infoprices,
    acts on stop_loss_pct / take_profit_pct from ETFRiskConfig
"""

import logging
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pnl_tracker
from typing import List, Optional

from core.saxo_client import SaxoClient
from core.etf_state import ETFStateStore
from core.etf_strategy import ETFSignal
from config.etf_config import ETFConfig

logger = logging.getLogger("etf_strategy.executor")


class ETFExecutor:
    def __init__(self, client: SaxoClient, state: ETFStateStore, cfg: ETFConfig):
        self.client = client
        self.state  = state
        self.cfg    = cfg
        self._account_key = cfg.risk.etf_account_key or self._discover_account_key()

    # ------------------------------------------------------------------
    # Account key discovery
    # ------------------------------------------------------------------

    def _discover_account_key(self) -> str:
        try:
            resp = self.client.get("/port/v1/accounts/me")
            data = resp.get("Data", resp)
            acct = data[0] if isinstance(data, list) else data
            key  = acct.get("AccountKey", "") if isinstance(acct, dict) else ""
            if key:
                logger.info(f"ETF: auto-discovered AccountKey ...{key[-6:]}")
            return key
        except Exception as exc:
            logger.warning(f"ETF: could not auto-discover AccountKey: {exc}")
            return ""

    # ------------------------------------------------------------------
    # Cash
    # ------------------------------------------------------------------

    def get_account_cash(self) -> float:
        try:
            resp = self.client.get("/port/v1/balances/me")
            # CashAvailableForTrading may be absent on some account types;
            # fall back to CashBalance (liquid cash) then MarginAvailableForTrading.
            cash = (resp.get("CashAvailableForTrading")
                    or resp.get("CashBalance")
                    or resp.get("MarginAvailableForTrading")
                    or 0)
            return float(cash)
        except Exception as exc:
            logger.warning(f"Could not fetch ETF account cash: {exc}")
            return 0.0

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

    def process_signals(self, signals: List[ETFSignal]) -> None:
        open_positions = self.state.position_count()
        slots_free     = max(0, self.cfg.risk.max_positions - open_positions)
        if slots_free == 0:
            logger.info("ETF position cap reached — no new entries this run")
            return

        cash = self.get_account_cash()
        if cash <= 0:
            logger.warning("ETF: cash balance is 0 — skipping entries")
            return

        allocation_budget  = cash * self.cfg.risk.total_allocation_pct_of_account
        per_position_limit = cash * self.cfg.risk.max_position_pct
        per_position_budget = min(
            allocation_budget / max(1, slots_free),
            per_position_limit,
        )

        logger.info(f"ETF budget: {allocation_budget:.0f} cash-ccy  "
                    f"({slots_free} free slots, {per_position_budget:.0f} per position)")

        for signal in signals[:slots_free]:
            if self.state.get_position(signal.uic):
                logger.debug(f"Already holding {signal.symbol} — skipping")
                continue
            self._enter_position(signal, per_position_budget)

    def _enter_position(self, signal: ETFSignal, budget_ccy: float) -> None:
        if budget_ccy <= 0:
            return

        price    = signal.last_price or signal.fast_ma  # last close is the best proxy
        quantity = int(budget_ccy // price) if price else 0
        if quantity <= 0:
            logger.info(f"Skipping {signal.symbol}: budget {budget_ccy:.0f} too small "
                        f"for 1 share at ~{price:.2f}")
            return

        order = {
            "AccountKey":    self._account_key,
            "Uic":           signal.uic,
            "AssetType":     "Etf",
            "Amount":        quantity,
            "BuySell":       "Buy",
            "OrderType":     "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder":   False,   # required by Saxo — marks algorithmic origin
        }

        if self.cfg.dry_run:
            logger.info(f"[DRY RUN] Would BUY {quantity}x {signal.symbol} "
                        f"(UIC {signal.uic}) score={signal.score:.3f} "
                        f"~{budget_ccy:.0f} {signal.currency} @ ~{price:.2f}")
            order_id = "DRY_RUN"
        else:
            resp     = self.client.post("/trade/v2/orders", json_body=order)
            order_id = resp.get("OrderId", "UNKNOWN")
            logger.info(f"ETF BUY {order_id}: {quantity}x {signal.symbol} @ ~{price:.2f}")

        self.state.upsert_position(signal.uic, {
            "symbol":      signal.symbol,
            "quantity":    quantity,
            "entry_price": price,          # stored so review_exits() can compute P&L
            "entry_score": signal.score,
            "order_id":    order_id,
        })
        self.state.log_order({
            "uic": signal.uic, "symbol": signal.symbol,
            "side": "Buy", "quantity": quantity,
            "entry_price": price, "order_id": order_id,
            "dry_run": self.cfg.dry_run,
        })
        if not self.cfg.dry_run:
            pnl_tracker.log_open("etf", "ETF Rotation", signal.symbol, "Buy",
                                 quantity, price, order_id=order_id)

    # ------------------------------------------------------------------
    # Exits — stop-loss / take-profit
    # ------------------------------------------------------------------

    def review_exits(self) -> None:
        """
        Fetches live prices via /trade/v1/infoprices and closes positions
        that have hit their stop-loss or take-profit threshold.
        Respects dry_run — logs the intent but skips the actual SELL order.
        """
        positions = self.state.all_positions()
        if not positions:
            return

        sl = self.cfg.risk.stop_loss_pct
        tp = self.cfg.risk.take_profit_pct

        for uic_str, pos in list(positions.items()):
            uic         = int(uic_str)
            symbol      = pos.get("symbol", uic_str)
            entry_price = pos.get("entry_price")
            if not entry_price:
                logger.warning(f"No entry_price recorded for {symbol} — skipping exit check")
                continue

            live = self._get_live_price(uic, symbol)
            if live is None:
                continue

            pnl = (live - entry_price) / entry_price
            logger.debug(f"{symbol}: entry={entry_price:.2f}  live={live:.2f}  P&L={pnl*100:+.1f}%  "
                         f"SL={-sl*100:.0f}%  TP=+{tp*100:.0f}%")

            if pnl <= -sl:
                self._exit_position(uic, pos, live, f"STOP_LOSS ({pnl*100:.1f}%)")
            elif pnl >= tp:
                self._exit_position(uic, pos, live, f"TAKE_PROFIT (+{pnl*100:.1f}%)")

    def _get_live_price(self, uic: int, symbol: str) -> Optional[float]:
        try:
            params = {"Uic": uic, "AssetType": "Etf", "FieldGroups": "Quote"}
            if self._account_key:
                params["AccountKey"] = self._account_key
            resp = self.client.get("/trade/v1/infoprices", params=params)
            q    = resp.get("Quote", {})
            mid  = q.get("Mid")
            if mid is None and q.get("Ask") and q.get("Bid"):
                mid = (float(q["Ask"]) + float(q["Bid"])) / 2
            if mid is None:
                mid = q.get("LastTraded")
            return float(mid) if mid else None
        except Exception as exc:
            logger.warning(f"Live price fetch failed for {symbol} UIC {uic}: {exc}")
            return None

    def _exit_position(self, uic: int, pos: dict, live_price: float, reason: str) -> None:
        quantity = pos.get("quantity", 0)
        symbol   = pos.get("symbol", str(uic))

        order = {
            "AccountKey":    self._account_key,
            "Uic":           uic,
            "AssetType":     "Etf",
            "Amount":        quantity,
            "BuySell":       "Sell",
            "OrderType":     "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder":   False,
        }

        if self.cfg.dry_run:
            logger.info(f"[DRY RUN] Would SELL {quantity}x {symbol} — {reason} @ ~{live_price:.2f}")
        else:
            resp = self.client.post("/trade/v2/orders", json_body=order)
            logger.info(f"ETF SELL {resp.get('OrderId','?')}: {quantity}x {symbol} — {reason} @ ~{live_price:.2f}")

        self.state.remove_position(uic)
        self.state.log_order({
            "uic": uic, "symbol": symbol,
            "side": "Sell", "quantity": quantity,
            "exit_price": live_price, "reason": reason,
            "dry_run": self.cfg.dry_run,
        })
        if not self.cfg.dry_run:
            pnl_tracker.log_close("etf", symbol, live_price, reason, strategy="ETF Rotation")
