"""
Order execution for the ETF strategy.

Uses its own capital allocation (config.risk.*) — entirely separate from the
shares strategies' capital and risk budget. Set dry_run=True (the default)
to log intended orders without sending them to the broker.

BROKER: executes via IBKR (ibkr_client.py / ibkr_order.py), not Saxo.
Signal generation and universe discovery (core/etf_universe.py,
core/etf_strategy.py) are UNCHANGED and stay on the Saxo `client` passed
in here — Saxo's instrument catalog is still the only source for "list
every ETF you offer", IBKR has no equivalent to page. Only *execution*
(orders, balances, positions, exit-price checks) moved to IBKR. That means
every ETFSignal arrives with a Saxo Uic (signal.uic) that this executor
never uses for trading — it resolves signal.symbol against IBKR directly
via ibkr_client.find_instrument() instead, and all position state below is
keyed by the resulting IBKR conId, not the Saxo Uic.

Key fixes vs. the original scaffold:
  - ManualOrder: False added to all orders (required by Saxo since ~2024) --
    n/a under IBKR, ibkr_client/ibkr_order have no such field
  - AccountKey auto-discovered if not set in config
  - entry_price stored in position state so review_exits() can compute P&L
  - review_exits() fully implemented: live price via ibkr_price_service,
    acts on stop_loss_pct / take_profit_pct from ETFRiskConfig
"""

import logging
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pnl_tracker
import trade_logger
import ibkr_client
import ibkr_order
import ibkr_price_service
from typing import List, Optional

from core.saxo_client import SaxoClient
from core.etf_state import ETFStateStore
from core.etf_strategy import ETFSignal
from config.etf_config import ETFConfig

logger = logging.getLogger("etf_strategy.executor")


class ETFExecutor:
    def __init__(self, client: SaxoClient, state: ETFStateStore, cfg: ETFConfig):
        self.client = client   # Saxo -- kept for signature compat with ETFBot; unused for execution now
        self.state  = state
        self.cfg    = cfg
        self._account_key = cfg.risk.etf_account_key or self._discover_account_key()
        self._ibkr_uic_cache: dict[str, Optional[int]] = {}

    # ------------------------------------------------------------------
    # Account key discovery
    # ------------------------------------------------------------------

    def _discover_account_key(self) -> str:
        try:
            key = ibkr_client.get_account_key()
            logger.info(f"ETF: IBKR account {key}")
            return key
        except Exception as exc:
            logger.warning(f"ETF: could not discover IBKR account id: {exc}")
            return ""

    # ------------------------------------------------------------------
    # Saxo Uic -> IBKR conId resolution
    # ------------------------------------------------------------------

    def _resolve_ibkr_uic(self, signal: ETFSignal) -> Optional[int]:
        """ETFSignal.uic is a Saxo Uic (from the Saxo-sourced universe/signal
        pipeline, unchanged) -- resolve the symbol against IBKR directly
        rather than depending on a pre-built etf_map_ibkr.csv, so this
        works without having run resolve_etf_universe_ibkr.py first.
        Cached per-run since the same symbol may appear across calls."""
        if signal.symbol in self._ibkr_uic_cache:
            return self._ibkr_uic_cache[signal.symbol]
        try:
            matches = ibkr_client.find_instrument(signal.symbol, asset_type="Etf")
            conid = matches[0]["Uic"] if matches else None
        except Exception as exc:
            logger.warning(f"ETF: IBKR lookup failed for {signal.symbol}: {exc}")
            conid = None
        if conid is None:
            logger.warning(f"ETF: {signal.symbol} (Saxo Uic {signal.uic}) not "
                            f"resolvable on IBKR -- skipping")
        self._ibkr_uic_cache[signal.symbol] = conid
        return conid

    # ------------------------------------------------------------------
    # Cash
    # ------------------------------------------------------------------

    def get_account_cash(self) -> float:
        try:
            bal = ibkr_client.get_balances()
            return float(bal.get("CashAvailableForTrading") or 0)
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

        # Iterate ALL signals (not just top-N) so that already-held positions
        # don't waste free slots — stop only when slots_free entries have been made.
        # Matched by symbol, not uic: state is keyed by IBKR conId (resolved at
        # entry time), while signal.uic is a Saxo Uic -- symbol is the only
        # identifier both sides agree on before resolution happens.
        already_held_symbols = {p.get("symbol") for p in self.state.all_positions().values()}
        slots_filled = 0
        for signal in signals:
            if slots_filled >= slots_free:
                break
            if signal.symbol in already_held_symbols:
                logger.debug(f"Already holding {signal.symbol} — skipping")
                continue
            self._enter_position(signal, per_position_budget)
            slots_filled += 1

    def _enter_position(self, signal: ETFSignal, budget_ccy: float) -> None:
        if budget_ccy <= 0:
            return

        ibkr_uic = self._resolve_ibkr_uic(signal)
        if ibkr_uic is None and not self.cfg.dry_run:
            return   # can't place a live order without a resolved conId

        price    = signal.last_price or signal.fast_ma  # last close is the best proxy
        quantity = int(budget_ccy // price) if price else 0
        if quantity <= 0:
            logger.info(f"Skipping {signal.symbol}: budget {budget_ccy:.0f} too small "
                        f"for 1 share at ~{price:.2f}")
            return

        stop_price = round(price * (1 - self.cfg.risk.stop_loss_pct), 4)
        tp_price   = round(price * (1 + self.cfg.risk.take_profit_pct), 2)
        stop_oid   = None
        tp_oid     = None

        if self.cfg.dry_run:
            logger.info(f"[DRY RUN] Would BUY {quantity}x {signal.symbol} "
                        f"(IBKR conId {ibkr_uic}) score={signal.score:.3f} "
                        f"~{budget_ccy:.0f} {signal.currency} @ ~{price:.2f}  "
                        f"stop={stop_price:.2f}  tp={tp_price:.2f}")
            order_id = "DRY_RUN"
        else:
            order_id, stop_oid, tp_oid = ibkr_order.place_with_stop(
                account_key       = self._account_key,
                uic               = ibkr_uic,
                asset_type        = "Etf",
                amount            = quantity,
                buy_sell          = "Buy",
                stop_price        = stop_price,
                label             = signal.symbol,
                take_profit_price = tp_price,
            )
            tp_info = f"  tp={tp_price:.2f} tp_order={tp_oid}" if tp_oid else ""
            logger.info(f"ETF BUY {order_id}: {quantity}x {signal.symbol} @ ~{price:.2f}  "
                        f"stop={stop_price:.2f}  stop_order={stop_oid}{tp_info}")

        # Keyed by IBKR conId (not signal.uic, which is a Saxo Uic) so
        # _sync_with_ibkr() can match this against ibkr_client.get_positions().
        state_key = ibkr_uic if ibkr_uic is not None else f"DRYRUN:{signal.symbol}"
        self.state.upsert_position(state_key, {
            "symbol":        signal.symbol,
            "saxo_uic":      signal.uic,   # informational only -- not used for trading
            "quantity":      quantity,
            "entry_price":   price,
            "stop_price":    stop_price,    # persisted so review_exits() uses entry stop, not current config
            "tp_price":      tp_price,
            "entry_score":   signal.score,
            "order_id":      order_id,
            "stop_order_id": stop_oid,
            "tp_order_id":   tp_oid,
        })
        self.state.log_order({
            "uic": state_key, "symbol": signal.symbol,
            "side": "Buy", "quantity": quantity,
            "entry_price": price, "order_id": order_id,
            "dry_run": self.cfg.dry_run,
        })
        trade_logger.log_trade(
            module   = "etf",
            strategy = "ETF Rotation",
            symbol   = signal.symbol,
            side     = "Buy",
            quantity = quantity,
            price    = price,
            order_id = order_id if not self.cfg.dry_run else None,
            dry_run  = self.cfg.dry_run,
        )
        if not self.cfg.dry_run:
            pnl_tracker.log_open("etf", "ETF Rotation", signal.symbol, "Buy",
                                 quantity, price, order_id=order_id,
                                 asset_type="ETF")

    # ------------------------------------------------------------------
    # IBKR position sync — removes phantom state from GTC-triggered exits
    # ------------------------------------------------------------------

    def _sync_with_ibkr(self) -> int:
        """
        Cross-check local state against IBKR's actual open positions.
        Removes any positions from state that IBKR no longer has (closed by
        the native stop/TP bracket, or manually). Returns number removed.
        Must be called BEFORE review_exits to prevent phantom SELL orders.

        State keys that aren't a plain conId (the "DRYRUN:SYMBOL" keys
        _enter_position() writes when dry_run placed no real order) are
        never real IBKR positions and are skipped here.
        """
        if self.cfg.dry_run:
            return 0   # no IBKR positions were ever opened in dry_run; nothing to sync
        try:
            resp = ibkr_client.get_positions()
            ibkr_conids = {row["Uic"] for row in resp.get("Data", [])}
        except Exception as exc:
            logger.warning(f"[sync] Could not fetch IBKR positions: {exc} — skipping sync")
            return 0

        removed = 0
        for key in list(self.state.all_positions()):
            if not key.isdigit():
                continue   # DRYRUN:SYMBOL keys -- never a real IBKR position
            if int(key) not in ibkr_conids:
                pos = self.state.get_position(key)
                label = pos.get("symbol", key) if pos else key
                logger.info(f"[sync] {label} no longer open in IBKR — removing phantom state")
                self.state.remove_position(key)
                removed += 1

        if removed:
            logger.info(f"[sync] Removed {removed} phantom position(s) from state")
        return removed

    # ------------------------------------------------------------------
    # Exits — stop-loss / take-profit (soft check after GTC sync)
    # ------------------------------------------------------------------

    def review_exits(self) -> None:
        """
        1. Sync state with IBKR — remove any positions already closed by the
           native stop/TP bracket.
        2. For remaining live positions, fetch current price and close those
           that breached stop_loss_pct or take_profit_pct (safety net for
           bracket-order failures).
        Respects dry_run — logs intent but skips actual SELL in dry mode.
        """
        # Remove phantom positions before checking exits — prevents duplicate SELL orders
        self._sync_with_ibkr()

        positions = self.state.all_positions()
        if not positions:
            return

        for key, pos in list(positions.items()):
            symbol      = pos.get("symbol", key)
            entry_price = pos.get("entry_price")
            if not entry_price:
                logger.warning(f"No entry_price recorded for {symbol} — skipping exit check")
                continue

            # Use persisted stop/tp prices (not current config) to avoid config-drift errors
            sl_price = pos.get("stop_price") or (entry_price * (1 - self.cfg.risk.stop_loss_pct))
            tp_price = pos.get("tp_price")   or (entry_price * (1 + self.cfg.risk.take_profit_pct))

            live = self._get_live_price(key, symbol)
            if live is None:
                continue

            pnl = (live - entry_price) / entry_price
            logger.debug(f"{symbol}: entry={entry_price:.2f}  live={live:.2f}  P&L={pnl*100:+.1f}%  "
                         f"SL={sl_price:.2f}  TP={tp_price:.2f}")

            if live <= sl_price:
                self._exit_position(key, pos, live, f"STOP_LOSS ({pnl*100:.1f}%)")
            elif live >= tp_price:
                self._exit_position(key, pos, live, f"TAKE_PROFIT (+{pnl*100:.1f}%)")

    def _get_live_price(self, key: str, symbol: str) -> Optional[float]:
        if not key.isdigit():
            return None   # DRYRUN:SYMBOL -- no real conId to price
        try:
            prices, _status = ibkr_price_service.fetch_prices(
                [{"symbol": symbol, "uic": int(key)}]
            )
            return prices.get(symbol)
        except Exception as exc:
            logger.warning(f"Live price fetch failed for {symbol} conId {key}: {exc}")
            return None

    def _exit_position(self, key: str, pos: dict, live_price: float, reason: str) -> None:
        quantity = pos.get("quantity", 0)
        symbol   = pos.get("symbol", key)

        if self.cfg.dry_run:
            logger.info(f"[DRY RUN] Would SELL {quantity}x {symbol} — {reason} @ ~{live_price:.2f}")
        else:
            # This is a runner-driven exit (stop/TP check here found a breach),
            # separate from the bracket placed at entry -- cancel the bracket's
            # resting legs first so they don't sit as orphaned orders that could
            # open an unintended reverse position if later triggered.
            for oid in (pos.get("stop_order_id"), pos.get("tp_order_id")):
                if oid and oid not in ("synced", None, ""):
                    ibkr_client.cancel_order(oid)
            resp = ibkr_client.place_market_order(int(key), "Etf", "Sell", quantity)
            logger.info(f"ETF SELL {resp.get('OrderId','?')}: {quantity}x {symbol} — {reason} @ ~{live_price:.2f}")

        self.state.remove_position(key)
        self.state.log_order({
            "uic": key, "symbol": symbol,
            "side": "Sell", "quantity": quantity,
            "exit_price": live_price, "reason": reason,
            "dry_run": self.cfg.dry_run,
        })
        trade_logger.log_trade(
            module   = "etf",
            strategy = "ETF Rotation",
            symbol   = symbol,
            side     = "Sell",
            quantity = quantity,
            price    = live_price,
            dry_run  = self.cfg.dry_run,
            notes    = reason,
        )
        if not self.cfg.dry_run:
            pnl_tracker.log_close("etf", symbol, live_price, reason, strategy="ETF Rotation",
                                  asset_type="ETF")
