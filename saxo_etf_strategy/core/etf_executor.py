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

import csv
import logging
import sys, os
from datetime import date, datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import pnl_tracker
import trade_logger
import saxo_order
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

        # Rank-weighted allocation, not an equal split. signals is already
        # ranked best-to-worst by the strategy (index 0 = strongest score) --
        # linear weight by rank so #1 gets the largest slice of the fixed
        # 15%-of-cash budget and the weakest candidate gets the smallest,
        # rather than every rank getting an identical dollar amount.
        # Requested 2026-08-24 alongside widening max_candidates_per_run
        # 3->10: testing more candidates shouldn't mean betting on the
        # 10th-best idea as hard as the 1st-best one, and spreading the
        # same total budget across more names naturally keeps every single
        # position small (more room stays free for other modules).
        n = len(signals)
        weights = [n - i for i in range(n)]          # rank0 -> n, rank(n-1) -> 1
        weight_sum = sum(weights) or 1

        if n:
            logger.info(f"ETF budget: {allocation_budget:.0f} cash-ccy  "
                        f"({slots_free} free slots, {n} ranked candidates, rank-weighted — "
                        f"top pick gets ~{allocation_budget*weights[0]/weight_sum:.0f}, "
                        f"bottom pick gets ~{allocation_budget*weights[-1]/weight_sum:.0f})")
        else:
            logger.info(f"ETF budget: {allocation_budget:.0f} cash-ccy "
                        f"({slots_free} free slots, 0 candidates)")

        # Iterate ALL signals (not just top-N) so that already-held positions
        # don't waste free slots — stop only when slots_free entries have been made.
        slots_filled = 0
        for rank, signal in enumerate(signals):
            if slots_filled >= slots_free:
                break
            if self.state.get_position(signal.uic):
                logger.debug(f"Already holding {signal.symbol} — skipping")
                continue
            weight = weights[rank] if rank < len(weights) else 1
            per_position_budget = min(
                allocation_budget * weight / weight_sum,
                per_position_limit,
            )
            self._enter_position(signal, per_position_budget, entry_rank=rank + 1)
            slots_filled += 1

    def _enter_position(self, signal: ETFSignal, budget_ccy: float, entry_rank: int = None) -> None:
        if budget_ccy <= 0:
            return

        # SIM per-trade notional cap (2026-09-01, user): SIM is for testing,
        # not size. ETF budget is sized off the raw SIM cash (~EUR 646k of
        # demo credit); clamp it to config/capital.json
        # account.sim_max_trade_notional_eur. Account ccy is EUR on SIM so
        # this is a direct comparison. 0/absent = disabled.
        try:
            from atos.capital_config import sim_max_trade_notional_eur
            _cap = sim_max_trade_notional_eur()
            if _cap and 0 < _cap < budget_ccy:
                logger.info(f"ETF {signal.symbol}: SIM notional cap "
                            f"{budget_ccy:.0f} -> {_cap:.0f} EUR")
                budget_ccy = _cap
        except Exception:
            pass

        price    = signal.last_price or signal.fast_ma  # last close is the best proxy
        # Capped at 50 shares/name — added 2026-08-22 at user's request. A
        # flat dollar budget alone sized cheap ETFs (XLF, XLE at $50-60) into
        # 400-500+ share positions, tying up disproportionate margin for
        # their dollar size versus a pricier ETF at the same budget. Matches
        # the same cap just added to US Blend's plan_rebalance() — keeps
        # per-name capital committed small so more margin stays free for
        # testing the forex module broadly, the current priority.
        MAX_SHARES_PER_NAME = 50
        quantity = min(int(budget_ccy // price), MAX_SHARES_PER_NAME) if price else 0
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

        stop_price = round(price * (1 - self.cfg.risk.stop_loss_pct), 4)
        tp_price   = round(price * (1 + self.cfg.risk.take_profit_pct), 2)
        stop_oid   = None
        tp_oid     = None

        if self.cfg.dry_run:
            logger.info(f"[DRY RUN] Would BUY {quantity}x {signal.symbol} "
                        f"(UIC {signal.uic}) score={signal.score:.3f} "
                        f"~{budget_ccy:.0f} {signal.currency} @ ~{price:.2f}  "
                        f"stop={stop_price:.2f}  tp={tp_price:.2f}")
            order_id = "DRY_RUN"
        else:
            def _client_post(path, body):
                return self.client.post(path, json_body=body)

            order_id, stop_oid, tp_oid = saxo_order.place_with_stop(
                post_fn           = _client_post,
                account_key       = self._account_key,
                uic               = signal.uic,
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

        self.state.upsert_position(signal.uic, {
            "symbol":        signal.symbol,
            "quantity":      quantity,
            "entry_price":   price,
            "entry_date":    date.today().isoformat(),   # dashboard "Days" column reads this
            "stop_price":    stop_price,    # persisted so review_exits() uses entry stop, not current config
            "tp_price":      tp_price,
            "entry_score":   signal.score,
            # 1-indexed position in that day's rotation ranking (1 = strongest
            # candidate) -- the same "rank" process_signals() used to weight
            # this position's budget allocation, persisted so the dashboard
            # can show "Top N" instead of only the raw score. Added
            # 2026-08-26 alongside the rank-performance log below; positions
            # entered before this field existed fall back to a score-sort
            # in the dashboard.
            "entry_rank":    entry_rank,
            "order_id":      order_id,
            "stop_order_id": stop_oid,
            "tp_order_id":   tp_oid,
        })
        self.state.log_order({
            "uic": signal.uic, "symbol": signal.symbol,
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
    # Saxo position sync — removes phantom state from GTC-triggered exits
    # ------------------------------------------------------------------

    def _sync_with_saxo(self) -> int:
        """
        Cross-check local state against Saxo's actual open ETF positions.
        Removes any positions from state that Saxo no longer has (closed by GTC
        stop/TP or manual close since last run). Returns number removed.
        Must be called BEFORE review_exits to prevent phantom SELL orders.
        """
        if self.cfg.dry_run:
            return 0   # no Saxo call in dry_run; nothing to sync
        try:
            resp = self.client.get("/port/v1/netpositions/me", params={
                "AssetType": "Etf",
                "FieldGroups": "NetPositionBase",
            })
            saxo_uics: set[int] = set()
            for row in resp.get("Data", []):
                base   = row.get("NetPositionBase", {})
                uic    = base.get("Uic")
                amount = float(base.get("Amount", 0) or 0)
                if uic is not None and abs(amount) > 0:
                    saxo_uics.add(int(uic))
        except Exception as exc:
            logger.warning(f"[sync] Could not fetch Saxo ETF positions: {exc} — skipping sync")
            return 0

        removed = 0
        for uic_str in list(self.state.all_positions()):
            if int(uic_str) not in saxo_uics:
                sym = self.state.get_position(int(uic_str) if uic_str.isdigit() else 0)
                label = sym.get("symbol", uic_str) if sym else uic_str
                logger.info(f"[sync] {label} no longer open in Saxo — removing phantom state")
                self.state.remove_position(int(uic_str))
                removed += 1

        if removed:
            logger.info(f"[sync] Removed {removed} phantom position(s) from state")
        return removed

    # ------------------------------------------------------------------
    # Ranking trim — sell any position not in the current top-N
    # ------------------------------------------------------------------

    def trim_out_of_ranking(self, signals) -> int:
        """Sell every open position whose symbol is NOT in the current
        top-N (max_candidates_per_run) signal list.  Called after
        generate_signals() so the list reflects today's ranking.
        Returns the number of positions sold."""
        top_symbols = {s.symbol for s in signals}
        positions   = self.state.all_positions()
        sold        = 0
        for uic_str, pos in list(positions.items()):
            uic    = int(uic_str)
            symbol = pos.get("symbol", uic_str)
            if symbol in top_symbols:
                continue  # still in top-N — keep
            live_price = self._live_price(uic, pos)
            if live_price is None:
                logger.warning(f"[trim] {symbol}: can't fetch live price — skipping trim")
                continue
            self._exit_position(uic, pos, live_price,
                                f"RANK_TRIM (dropped out of top {self.cfg.strategy.max_candidates_per_run})")
            sold += 1
        if sold:
            logger.info(f"[trim] Sold {sold} position(s) that dropped out of the top-"
                        f"{self.cfg.strategy.max_candidates_per_run} ranking")
        return sold

    # ------------------------------------------------------------------
    # Exits — stop-loss / take-profit (soft check after GTC sync)
    # ------------------------------------------------------------------

    def review_exits(self) -> None:
        """
        1. Sync state with Saxo — remove any positions already closed by GTC orders.
        2. For remaining live positions, fetch current price and close those that
           breached stop_loss_pct or take_profit_pct (safety net for GTC failures).
        Respects dry_run — logs intent but skips actual SELL in dry mode.
        """
        # Remove phantom positions before checking exits — prevents duplicate SELL orders
        self._sync_with_saxo()

        positions = self.state.all_positions()
        if not positions:
            return

        for uic_str, pos in list(positions.items()):
            uic         = int(uic_str)
            symbol      = pos.get("symbol", uic_str)
            entry_price = pos.get("entry_price")
            if not entry_price:
                logger.warning(f"No entry_price recorded for {symbol} — skipping exit check")
                continue

            # Use persisted stop/tp prices (not current config) to avoid config-drift errors
            sl_price = pos.get("stop_price") or (entry_price * (1 - self.cfg.risk.stop_loss_pct))
            tp_price = pos.get("tp_price")   or (entry_price * (1 + self.cfg.risk.take_profit_pct))

            live = self._get_live_price(uic, symbol)
            if live is None:
                continue

            pnl = (live - entry_price) / entry_price
            logger.debug(f"{symbol}: entry={entry_price:.2f}  live={live:.2f}  P&L={pnl*100:+.1f}%  "
                         f"SL={sl_price:.2f}  TP={tp_price:.2f}")

            if live <= sl_price:
                self._exit_position(uic, pos, live, f"STOP_LOSS ({pnl*100:.1f}%)")
            elif live >= tp_price:
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

    # ------------------------------------------------------------------
    # Rank/performance history
    # ------------------------------------------------------------------

    _RANK_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "data", "etf_rank_performance_log.csv")
    _RANK_LOG_FIELDS = ["timestamp", "symbol", "rank", "entry_score", "entry_price",
                         "entry_date", "days_held", "current_price", "pnl_pct", "pnl_usd"]

    def log_rank_performance(self) -> None:
        """
        Append one row per currently-held position to a running CSV so how
        each rotation rank (Top 1 = strongest pick that day, down to Top 10)
        actually performs over time can be analyzed later, not just seen as
        a single dashboard snapshot. Called once per bot run (hourly) --
        legacy positions entered before entry_rank existed log rank="" and
        are still worth keeping for their performance trail.
        """
        positions = self.state.all_positions()
        if not positions:
            return

        now = datetime.now().isoformat(timespec="seconds")
        rows = []
        for uic_str, pos in positions.items():
            uic         = int(uic_str)
            symbol      = pos.get("symbol", uic_str)
            entry_price = pos.get("entry_price")
            live        = self._get_live_price(uic, symbol) if entry_price else None
            pnl_pct     = (live - entry_price) / entry_price * 100 if live and entry_price else None
            pnl_usd     = (live - entry_price) * pos.get("quantity", 0) if live and entry_price else None
            rows.append({
                "timestamp":     now,
                "symbol":        symbol,
                "rank":          pos.get("entry_rank", ""),
                "entry_score":   pos.get("entry_score", ""),
                "entry_price":   entry_price,
                "entry_date":    pos.get("entry_date", ""),
                "days_held":     (date.today() - date.fromisoformat(pos["entry_date"])).days
                                 if pos.get("entry_date") else "",
                "current_price": live if live is not None else "",
                "pnl_pct":       round(pnl_pct, 3) if pnl_pct is not None else "",
                "pnl_usd":       round(pnl_usd, 2) if pnl_usd is not None else "",
            })

        os.makedirs(os.path.dirname(self._RANK_LOG_PATH), exist_ok=True)
        write_header = not os.path.exists(self._RANK_LOG_PATH)
        with open(self._RANK_LOG_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._RANK_LOG_FIELDS)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
