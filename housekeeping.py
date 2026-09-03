"""
housekeeping.py
----------------
Cross-module state reconciliation for Forex, Futures, ETF and Shares (ATOS).

Saxo is always the ground truth. This module pulls live positions and
orders ONCE per run, compares them against each module's local state file,
and fixes the specific class of drift discovered manually on 2026-08-24:

  1. Local state tracks a position that no longer has ANY live backing
     (fully netted away or closed by an opposite trade elsewhere) ->
     remove the local entry, cancel its now-orphaned stop order(s).
  2. Local state's combined quantity for an instrument EXCEEDS live
     exposure (Saxo silently netted part of it against another strategy's
     opposite trade) -> scale local quantities down proportionally so
     protective stops can never try to close more than actually exists.
  3. Duplicate stop orders on the same instrument/side/price (breakeven
     moves or race-condition retries that left the old order uncancelled)
     -> cancel all but the newest.
  4. Live exposure with NO local tracking at all, or local direction that
     is the OPPOSITE of live direction -> reported only, never guessed at
     automatically (see run() docstring for why).

Why this exists: Saxo nets opposite-direction trades on the same
instrument automatically. Local state assumes each strategy owns its own
independent position ticket. Whenever two strategies trade the same
symbol in opposite directions, or a scheduler race condition fires the
same signal twice, the two views drift apart silently — nothing crashes,
nothing logs an error, a stop order just ends up protecting a position
that no longer exists (or protecting less/more than actually exists).

Call reconcile_all() after every live trading run (wired into forex/
futures/etf/atos runners) AND on a periodic Task Scheduler safety-net run
(run_housekeeping.py) in case a run's own reconciliation pass is skipped.

scan_naked_positions() is a SEPARATE, read-only check: any live position
across all 4 modules with no working stop-loss order protecting it at
all. It does not touch local state and does not auto-fix anything — see
its docstring for why closing/protecting is a decision for a human.
"""

from __future__ import annotations

import logging
import os
import smtplib
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import saxo_client
import saxo_order

logger = logging.getLogger("housekeeping")

_ROOT = os.path.dirname(os.path.abspath(__file__))
_DATA = os.path.join(_ROOT, "data")
_EMAIL_CFG = os.path.join(_ROOT, "config", "email.json")

FOREX_STATE   = os.path.join(_DATA, "forex_state.json")
FUTURES_STATE = os.path.join(_DATA, "futures_state.json")
ETF_STATE     = os.path.join(_ROOT, "saxo_etf_strategy", "data", "etf_positions.json")
ATOS_DB       = os.path.join(_DATA, "atos_live.db")
ATOS_AI_DB    = os.path.join(_DATA, "atos_ai.db")    # AI SIM twin trades


# ── Normalized record ─────────────────────────────────────────────────────

@dataclass
class LocalPosition:
    module:        str            # "forex" | "futures" | "etf" | "stocks"
    key:           str            # module-native identifier (see each adapter)
    uic:           int
    symbol:        str
    direction:     str            # "Buy" or "Sell"
    quantity:      int
    asset_type:    str
    stop_order_id: str | None = None
    stop_price:    float = 0.0    # this entry's own intended risk level, straight from its
                                   # module's state file -- used by _check_stop_integrity() to
                                   # tell "no live order at all" from "a live order exists but
                                   # at a different price than intended" without guessing


@dataclass
class Finding:
    module:  str
    kind:    str                  # see KIND_* constants below
    symbol:  str
    detail:  str
    estimate: bool = False
    uic:     int = 0              # populated for KIND_FULLY_UNTRACKED (so it can be auto-closed on SIM)
    net_amount: float = 0.0      # signed live net for that uic (+long / -short)
    asset_type: str = ""


KIND_REMOVED_ORPHAN   = "removed_orphan"       # local tracked, no live backing -> removed
KIND_SCALED_DOWN      = "scaled_down"          # local overstated live exposure -> corrected
KIND_DUPLICATE_STOP   = "duplicate_stop"       # cancelled a literal duplicate order
KIND_DIRECTION_MISMATCH = "direction_mismatch" # local direction opposite of live -> flagged only
KIND_UNTRACKED_LIVE   = "untracked_live"       # live exposure with no local record at all
KIND_STOP_REPLACE_FAILED = "stop_replace_failed"
KIND_LEDGER_DRIFT      = "ledger_drift"        # stocks-only: can't auto-remove a ledger row
KIND_PENDING_ENTRY     = "pending_entry"       # matching entry order still Working, not filled yet -> left alone
KIND_FULLY_UNTRACKED   = "fully_untracked"     # live position, ZERO local record in ANY module -- structurally invisible to reconcile_module()
KIND_STOP_MISSING      = "stop_missing"        # local's remembered stop_order_id isn't a live Working order at all -> re-placed at local's own stop_price
KIND_STOP_STALE        = "stop_stale"          # a real Working order exists at that id, but its live price != local's stop_price -> local adopts the broker's real price (the broker order is what actually protects the position, so it's the ground truth), fixed and reported
KIND_SUSPECT_ORPHAN    = "suspect_orphan"      # live_net==0 but local's remembered stop is STILL Working -> contradiction, likely an incomplete/stale positions snapshot rather than a real close -> left alone, NOT removed/cancelled

# Order types that count as a real protective stop. "StopIfTraded" and
# "TrailingStopIfTraded" are Saxo's stop-market equivalents for instruments
# whose SupportedOrderTypes don't include plain "Stop"/"StopLimit" -- found
# live 2026-08-24 on ZC (corn), whose own instrument details list only
# ['TriggerBreakout', 'TriggerStop', 'TriggerLimit', 'StopIfTraded',
# 'TrailingStopIfTraded', 'Limit', 'Market'] despite sharing AssetType=
# ContractFutures with GC/ES (which DO accept plain Stop/StopLimit). Before
# this fix a StopIfTraded order protecting a position was invisible to
# working_stops()/scan_naked_positions(), producing a false "naked" alert
# for an already-protected position. See saxo_order._post_stop_order() for
# the matching order-placement-side fix.
_STOP_ORDER_TYPES = ("Stop", "StopLimit", "StopIfTraded", "TrailingStopIfTraded")


# ── Live Saxo snapshot (fetched once, shared by every module) ─────────────

@dataclass
class LiveSnapshot:
    positions_by_uic: dict            # uic -> list of raw position dicts (PositionBase+PositionView)
    orders_by_uic:    dict            # uic -> list of raw working-order dicts

    def net_amount(self, uic: int) -> float:
        return sum(p["PositionBase"]["Amount"] for p in self.positions_by_uic.get(uic, []))

    def working_stops(self, uic: int) -> list:
        return [o for o in self.orders_by_uic.get(uic, [])
                if o.get("OpenOrderType") in _STOP_ORDER_TYPES and o.get("Status") == "Working"]

    def has_pending_entry(self, uic: int, direction: str) -> bool:
        """True if a Working Market/Limit order exists in the OPENING
        direction for this uic — i.e. a bracket entry that hasn't filled
        yet (market closed, awaiting execution), not a closing/stop/tp leg.
        Found 2026-08-24: reconcile_module() saw zero live position for 7
        pending ETF entries (US market wasn't open yet) and correctly
        concluded "no live backing", but that's a DIFFERENT situation from
        a genuine orphan — the entry is real and about to fill. Removing
        the local entry and cancelling its still-dormant bracket stop leg
        on a position that's about to exist is actively harmful, and would
        repeat every ~30 min until the entry actually fills."""
        for o in self.orders_by_uic.get(uic, []):
            if (o.get("Status") == "Working" and o.get("BuySell") == direction
                    and o.get("OpenOrderType") in ("Market", "Limit", "StopLimit")
                    and o.get("OrderRelation") != "IfDoneSlave"):
                return True
        return False


def fetch_live_snapshot() -> LiveSnapshot:
    """SIM-only, as this module always was. The real-money LIVE account's
    equivalent (its own snapshot fetch, its own adapter, its own
    reconciliation entry point) lives entirely in housekeeping_live.py --
    a deliberately separate file, not an env parameter on this one, per
    explicit user direction that LIVE never share SIM's module/functions."""
    pos_resp = saxo_client.get_positions()
    positions = pos_resp.get("Data", pos_resp)
    ord_resp = saxo_client.get_orders()
    orders = ord_resp.get("Data", ord_resp)

    positions_by_uic: dict = {}
    for p in positions:
        uic = p["PositionBase"]["Uic"]
        positions_by_uic.setdefault(uic, []).append(p)

    orders_by_uic: dict = {}
    for o in orders:
        uic = o.get("Uic")
        orders_by_uic.setdefault(uic, []).append(o)

    return LiveSnapshot(positions_by_uic, orders_by_uic)


# ── Adapters ────────────────────────────────────────────────────────────
# Each adapter normalizes one module's local state into LocalPosition rows,
# writes corrected quantities back, and knows how to replace a protective
# stop for its own asset type using saxo_order.place_protective_stop().

class BaseAdapter:
    module: str
    asset_types: set[str]
    can_auto_remove: bool = True   # False for ledger-style stores (stocks)

    def load(self) -> list[LocalPosition]:
        raise NotImplementedError

    def save(self, positions: list[LocalPosition], removed_keys: list[str]) -> None:
        raise NotImplementedError

    def replace_stop(self, pos: LocalPosition, new_quantity: int, price: float) -> str | None:
        raise NotImplementedError

    def cancel_stop(self, order_id: str) -> bool:
        return saxo_client.cancel_order(order_id)


class ForexAdapter(BaseAdapter):
    module = "forex"
    asset_types = {"FxSpot"}

    def _import(self):
        import forex.runner as r
        return r

    def load(self) -> list[LocalPosition]:
        r = self._import()
        state = r._load_state()
        out = []
        for key, v in state.get("positions", {}).items():
            if v.get("paper"):
                # ATOS-simulated fill (Saxo SIM order engine was down) --
                # no Saxo counterpart, managed entirely by forex/runner.py's
                # own exit logic. Reconciliation must not see it or it flags
                # a phantom and deletes it. See runner._sim_paper_fill_enabled.
                continue
            symbol = key.split(":", 1)[1] if ":" in key else key
            out.append(LocalPosition(
                module=self.module, key=key, uic=v["uic"], symbol=symbol,
                direction=v.get("direction", "Buy"), quantity=int(v["quantity"]),
                asset_type="FxSpot", stop_order_id=v.get("stop_order_id"),
                stop_price=float(v.get("stop_price") or 0.0),
            ))
        return out

    def save(self, positions: list[LocalPosition], removed_keys: list[str]) -> None:
        r = self._import()
        state = r._load_state()
        for key in removed_keys:
            state["positions"].pop(key, None)
        for lp in positions:
            entry = state["positions"].get(lp.key)
            if entry is not None:
                entry["quantity"] = lp.quantity
                if lp.stop_order_id:
                    entry["stop_order_id"] = lp.stop_order_id
                if lp.stop_price:
                    entry["stop_price"] = lp.stop_price
        r._save_state(state)

    def replace_stop(self, pos: LocalPosition, new_quantity: int, price: float) -> str | None:
        r = self._import()
        akey = saxo_client.get_account_key()
        if pos.stop_order_id:
            self.cancel_stop(pos.stop_order_id)
        dp = r.get_price_decimals(pos.symbol)
        return saxo_order.place_protective_stop(
            post_fn=lambda path, body: r._post(path, body),
            account_key=akey, uic=pos.uic, asset_type="FxSpot",
            amount=new_quantity, direction=pos.direction, stop_price=price,
            label=f"{pos.key} (housekeeping)", symbol=pos.symbol, price_decimals=dp,
        )




class FuturesAdapter(BaseAdapter):
    module = "futures"
    asset_types = {"CfdOnIndex", "ContractFutures", "FxSpot"}

    def _import(self):
        import futures.runner as r
        return r

    # load()/save() read+write futures_state.json DIRECTLY rather than going
    # through futures.runner._load_state()/_save_state() -- importing that
    # module at all runs its top-level logging setup, which opens a
    # logging.FileHandler on logs/futures_{today}.log. Found live 2026-08-26:
    # reconcile_module("futures") is called from EVERY module's post-run
    # safeguard pass (forex/etf/stocks) plus the standalone Safeguard task,
    # so many separate processes were each importing futures.runner just to
    # read a JSON file -- and a single stray long-running python process that
    # had that FileHandler open (from an interactive session, never a normal
    # pythonw/Task-Scheduler run) made EVERY one of those imports fail with
    # "PermissionError: [Errno 13] Permission denied", crashing this
    # module's reconciliation on every single run all day. Reading the state
    # file directly (same shape ETFAdapter already uses) needs no logging
    # setup at all, so it can't collide with anything holding that log file
    # open. _import()/futures.runner is still used by replace_stop() below,
    # which genuinely needs the authenticated Saxo client.
    def load(self) -> list[LocalPosition]:
        import json
        if not os.path.exists(FUTURES_STATE):
            return []
        with open(FUTURES_STATE) as f:
            state = json.load(f)
        out = []
        for key, v in state.get("positions", {}).items():
            symbol = key.split(":", 1)[1] if ":" in key else key
            out.append(LocalPosition(
                module=self.module, key=key, uic=v["uic"], symbol=symbol,
                direction=v.get("direction", "Buy"), quantity=int(v["quantity"]),
                asset_type=v.get("asset_type", "FxSpot"), stop_order_id=v.get("stop_order_id"),
                stop_price=float(v.get("stop_price") or 0.0),
            ))
        return out

    def save(self, positions: list[LocalPosition], removed_keys: list[str]) -> None:
        import json
        if not os.path.exists(FUTURES_STATE):
            return
        with open(FUTURES_STATE) as f:
            state = json.load(f)
        for key in removed_keys:
            state.get("positions", {}).pop(key, None)
        for lp in positions:
            entry = state.get("positions", {}).get(lp.key)
            if entry is not None:
                entry["quantity"] = lp.quantity
                if lp.stop_order_id:
                    entry["stop_order_id"] = lp.stop_order_id
                if lp.stop_price:
                    entry["stop_price"] = lp.stop_price
        tmp = FUTURES_STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2, default=str)
        os.replace(tmp, FUTURES_STATE)

    def replace_stop(self, pos: LocalPosition, new_quantity: int, price: float) -> str | None:
        r = self._import()
        akey = saxo_client.get_account_key()
        if pos.stop_order_id:
            self.cancel_stop(pos.stop_order_id)
        return saxo_order.place_protective_stop(
            post_fn=lambda path, body: r._post(path, body),
            account_key=akey, uic=pos.uic, asset_type=pos.asset_type,
            amount=new_quantity, direction=pos.direction, stop_price=price,
            label=f"{pos.key} (housekeeping)", symbol=pos.symbol,
        )


class ETFAdapter(BaseAdapter):
    module = "etf"
    asset_types = {"Etf"}

    def load(self) -> list[LocalPosition]:
        import json
        if not os.path.exists(ETF_STATE):
            return []
        with open(ETF_STATE) as f:
            state = json.load(f)
        out = []
        for uic_str, v in state.get("positions", {}).items():
            out.append(LocalPosition(
                module=self.module, key=uic_str, uic=int(uic_str), symbol=v.get("symbol", uic_str),
                direction="Buy", quantity=int(v["quantity"]),
                asset_type="Etf", stop_order_id=v.get("stop_order_id"),
                stop_price=float(v.get("stop_price") or 0.0),
            ))
        return out

    def save(self, positions: list[LocalPosition], removed_keys: list[str]) -> None:
        import json
        if not os.path.exists(ETF_STATE):
            return
        with open(ETF_STATE) as f:
            state = json.load(f)
        for key in removed_keys:
            state["positions"].pop(key, None)
        for lp in positions:
            entry = state["positions"].get(lp.key)
            if entry is not None:
                entry["quantity"] = lp.quantity
                if lp.stop_order_id:
                    entry["stop_order_id"] = lp.stop_order_id
                if lp.stop_price:
                    entry["stop_price"] = lp.stop_price
        tmp = ETF_STATE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, ETF_STATE)

    def replace_stop(self, pos: LocalPosition, new_quantity: int, price: float) -> str | None:
        akey = saxo_client.get_account_key()
        if pos.stop_order_id:
            self.cancel_stop(pos.stop_order_id)
        return saxo_order.place_protective_stop(
            post_fn=lambda path, body: saxo_client.post(path, body),
            account_key=akey, uic=pos.uic, asset_type="Etf",
            amount=new_quantity, direction="Buy", stop_price=price,
            label=f"{pos.symbol} (housekeeping)", symbol=pos.symbol,
        )


class StocksAdapter(BaseAdapter):
    """ATOS shares (US Blend / US Reversion). Backed by a SQLite trade
    LEDGER (atos_live.db), not a simple position tracker — a row also
    carries realized P&L once closed. That makes "no live backing at all"
    fundamentally different here: for forex/futures/ETF, removing a stale
    entry just stops tracking it. For stocks, marking a row closed without
    a real exit_price/exit_date would fabricate ledger history. So this
    adapter WILL scale down an overstated `shares` count (safe — doesn't
    touch P&L fields), but will NEVER auto-remove/close a row with zero
    live backing; that case is reported (KIND_LEDGER_DRIFT) for a human to
    resolve with the real fill data.
    """
    module = "stocks"
    asset_types = {"Stock"}
    can_auto_remove = False

    def _imap(self):
        from instrument_map import load_instrument_map
        return load_instrument_map()

    def load(self) -> list[LocalPosition]:
        imap = self._imap()
        # Key by the Yahoo ticker (imap key), which is what atos_live.db stores
        # in the `ticker` column.  The old code used info.get("symbol","") which
        # is the Saxo format ('HUM:xnys') — a mismatch that silently returned []
        # for every DB row, making ALL real stock positions appear unattributable.
        by_ticker: dict[str, dict] = dict(imap)
        out: list[LocalPosition] = []
        # Read both the main ATOS SIM DB and the AI SIM twin DB so housekeeping
        # can see positions opened by atos_ai_stocks.py (which writes atos_ai.db
        # and uses the same Saxo SIM account).  Positions absent from both DBs
        # are genuinely unattributable; positions in either one are tracked.
        for db_path in (ATOS_DB, ATOS_AI_DB):
            if not os.path.exists(db_path):
                continue
            try:
                con = sqlite3.connect(db_path)
                con.row_factory = sqlite3.Row
                # COALESCE(paper,0)=0 -- skip locally-simulated fills (Saxo SIM
                # order engine was down; booked with paper=1 in atos_runner).
                rows = con.execute(
                    "SELECT id, ticker, direction, shares FROM trades "
                    "WHERE exit_date IS NULL AND COALESCE(paper, 0) = 0"
                ).fetchall()
                for row in rows:
                    info = by_ticker.get(row["ticker"])
                    if not info:
                        continue
                    direction = "Buy" if (row["direction"] or "BUY").upper() == "BUY" else "Sell"
                    out.append(LocalPosition(
                        module=self.module,
                        key=f"{os.path.basename(db_path)}:{row['id']}",
                        uic=info["uic"], symbol=row["ticker"],
                        direction=direction, quantity=int(row["shares"]),
                        asset_type="Stock", stop_order_id=None,
                    ))
                con.close()
            except Exception:
                pass
        return out

    def save(self, positions: list[LocalPosition], removed_keys: list[str]) -> None:
        # removed_keys are never populated for this adapter (can_auto_remove=False);
        # only quantity corrections are ever written back.
        con = sqlite3.connect(ATOS_DB)
        cur = con.cursor()
        for lp in positions:
            cur.execute("UPDATE trades SET shares = ? WHERE id = ?", (lp.quantity, int(lp.key)))
        con.commit()
        con.close()

    def replace_stop(self, pos: LocalPosition, new_quantity: int, price: float) -> str | None:
        # ATOS doesn't track stop_order_id locally at all (confirmed gap,
        # see [[atos_stocks_module]]) — nothing to cancel/replace here.
        # Missing protection on stock positions is caught by
        # scan_naked_positions() instead, which works purely off live data.
        return None


ADAPTERS = {
    "forex":   ForexAdapter(),
    "futures": FuturesAdapter(),
    "etf":     ETFAdapter(),
    "stocks":  StocksAdapter(),
}


# ── Core reconciliation ────────────────────────────────────────────────────

def _group_by_uic(positions: list[LocalPosition]) -> dict:
    groups: dict = {}
    for lp in positions:
        groups.setdefault(lp.uic, []).append(lp)
    return groups


def _signed(lp: LocalPosition) -> int:
    return lp.quantity if lp.direction == "Buy" else -lp.quantity


def reconcile_module(adapter: BaseAdapter, live: LiveSnapshot,
                     aggressive: bool = False) -> list[Finding]:
    """aggressive=True additionally removes local entries whose claimed
    direction has ZERO live backing (a direction_mismatch, not just a
    zero-exposure orphan) instead of only reporting them. Used by
    safeguard.py, which — unlike this function's own conservative default
    used by the unattended post-trade hooks — is explicitly asked to
    resolve every finding, not just the unambiguous ones. Safe because the
    entry is provably fictional in its own claimed direction regardless:
    the real live exposure it was confused with is a SEPARATE thing this
    function never touches."""
    findings: list[Finding] = []
    local = adapter.load()
    if not local:
        return findings

    groups = _group_by_uic(local)
    updated: list[LocalPosition] = []
    removed_keys: list[str] = []

    for uic, entries in groups.items():
        live_net = live.net_amount(uic)
        local_net = sum(_signed(e) for e in entries)
        symbol = entries[0].symbol

        if local_net == 0:
            continue

        if live_net == 0 and live.has_pending_entry(uic, entries[0].direction):
            # A Working entry order exists in the same direction the local
            # entries claim, but it hasn't filled yet (e.g. market closed) --
            # this is a pending trade, not an orphan. Defer judgment to a
            # later run rather than removing it and cancelling its still-
            # dormant bracket stop leg on a position that's about to exist.
            findings.append(Finding(
                adapter.module, KIND_PENDING_ENTRY, symbol,
                f"{entries[0].key}: no live position yet but a matching entry "
                f"order is still Working (not yet filled) — leaving as-is",
            ))
            continue

        if live_net == 0:
            # Nothing live backs any of these entries at all -- UNLESS one of
            # them still has a genuinely Working stop order at the broker,
            # which is a contradiction: Saxo cancels/fills a position's stop
            # order when that position actually closes, so a still-Working
            # stop is strong evidence the position is too, and live_net==0
            # is more likely an incomplete/stale positions snapshot than a
            # real close. Found live 2026-08-26: this branch wrongly removed
            # donchian:ZC and cancelled its real, still-Working stop
            # (5039822527) for a position that Saxo's own API confirmed was
            # STILL OPEN moments later -- a one-off bad snapshot read turned
            # into a real, hours-long naked position with no way to recover
            # it automatically (ZC's SIM quote restriction means safeguard's
            # naked-fix can't re-price a stop for it either). Checking the
            # order snapshot we already fetched (no extra API call) before
            # committing to "orphan" would have caught this before any
            # damage was done.
            suspect = [e for e in entries if e.stop_order_id and any(
                o.get("OrderId") == e.stop_order_id and o.get("Status") == "Working"
                for o in live.orders_by_uic.get(uic, []))]
            if suspect:
                findings.append(Finding(
                    adapter.module, KIND_SUSPECT_ORPHAN, symbol,
                    f"{suspect[0].key}: live position shows 0 net, but its remembered "
                    f"stop {suspect[0].stop_order_id} is still a Working order at the "
                    f"broker — Saxo would have cancelled/filled that stop if the "
                    f"position genuinely closed, so this looks like a stale/incomplete "
                    f"positions snapshot rather than a real close. NOT removed or "
                    f"cancelled — left as-is for the next run to re-check with a fresh snapshot.",
                ))
                continue
            for e in entries:
                if adapter.can_auto_remove:
                    if e.stop_order_id:
                        adapter.cancel_stop(e.stop_order_id)
                    removed_keys.append(e.key)
                    findings.append(Finding(
                        adapter.module, KIND_REMOVED_ORPHAN, symbol,
                        f"{e.key}: local {e.direction} {e.quantity} has no live backing "
                        f"at all (fully netted/closed elsewhere) — removed from state"
                        + (f", cancelled orphaned stop {e.stop_order_id}" if e.stop_order_id else "")
                    ))
                else:
                    findings.append(Finding(
                        adapter.module, KIND_LEDGER_DRIFT, symbol,
                        f"{e.key}: local {e.direction} {e.quantity} has no live backing at all, "
                        f"but this is a ledger row — NOT auto-closed. Needs a real exit price/date.",
                    ))
            continue

        if (live_net > 0) != (local_net > 0):
            if aggressive:
                for e in entries:
                    if adapter.can_auto_remove:
                        if e.stop_order_id:
                            adapter.cancel_stop(e.stop_order_id)
                        removed_keys.append(e.key)
                        findings.append(Finding(
                            adapter.module, KIND_DIRECTION_MISMATCH, symbol,
                            f"{e.key}: local {e.direction} {e.quantity} has ZERO live backing in "
                            f"that direction (live net {live_net:+,.0f} is entirely opposite-signed) "
                            f"— removed from state, cancelled its stop"
                            + (f" {e.stop_order_id}" if e.stop_order_id else ""),
                        ))
                    else:
                        findings.append(Finding(
                            adapter.module, KIND_LEDGER_DRIFT, symbol,
                            f"{e.key}: local {e.direction} {e.quantity} has ZERO live backing in "
                            f"that direction, but this is a ledger row — NOT auto-closed. Needs a "
                            f"real exit price/date.",
                        ))
            else:
                findings.append(Finding(
                    adapter.module, KIND_DIRECTION_MISMATCH, symbol,
                    f"live net {live_net:+,.0f} vs local net {local_net:+,.0f} — OPPOSITE direction, "
                    f"not auto-corrected (too ambiguous to guess safely)",
                ))
            continue

        if abs(live_net) < abs(local_net):
            # Local overstates real exposure -> scale every entry down
            # proportionally so total protective-stop coverage can never
            # exceed what's actually open.
            scale = abs(live_net) / abs(local_net)
            remaining = int(abs(live_net))
            for i, e in enumerate(entries):
                if i == len(entries) - 1:
                    new_qty = remaining
                else:
                    new_qty = int(round(e.quantity * scale))
                    remaining -= new_qty
                new_qty = max(new_qty, 0)
                if new_qty == e.quantity:
                    continue
                if new_qty == 0:
                    if adapter.can_auto_remove:
                        if e.stop_order_id:
                            adapter.cancel_stop(e.stop_order_id)
                        removed_keys.append(e.key)
                    findings.append(Finding(
                        adapter.module, KIND_SCALED_DOWN, symbol,
                        f"{e.key}: scaled from {e.quantity} to 0 (no remaining share of live "
                        f"exposure after proportional split) — removed", estimate=True,
                    ))
                    continue
                old_qty = e.quantity
                trigger_price = _entry_stop_price(adapter, e)
                new_oid = adapter.replace_stop(e, new_qty, trigger_price)
                if new_oid is None and e.stop_order_id and adapter.can_auto_remove:
                    findings.append(Finding(
                        adapter.module, KIND_STOP_REPLACE_FAILED, symbol,
                        f"{e.key}: quantity corrected {old_qty}->{new_qty} but replacement "
                        f"stop FAILED — position may be under/unprotected, check manually",
                    ))
                e.quantity = new_qty
                if new_oid:
                    e.stop_order_id = new_oid
                    if trigger_price:
                        # Persist the price the new stop was ACTUALLY placed at (read live,
                        # not invented) so local state can't silently drift from reality the
                        # way it did before save() wrote stop_order_id but never stop_price --
                        # see _check_stop_integrity()'s "stop_stale" finding, which is what
                        # catches this exact class of drift going forward.
                        e.stop_price = trigger_price
                updated.append(e)
                findings.append(Finding(
                    adapter.module, KIND_SCALED_DOWN, symbol,
                    f"{e.key}: local said {e.direction} {old_qty} but live exposure only "
                    f"supports {new_qty} (Saxo netted the rest against another strategy's "
                    f"opposite trade) — corrected, estimate from proportional split across "
                    f"{len(entries)} strateg{'y' if len(entries)==1 else 'ies'} on this symbol",
                    estimate=True,
                ))

        elif abs(live_net) > abs(local_net):
            findings.append(Finding(
                adapter.module, KIND_UNTRACKED_LIVE, symbol,
                f"live net {live_net:+,.0f} exceeds local net {local_net:+,.0f} by "
                f"{abs(live_net) - abs(local_net):,.0f} units — untracked live exposure exists "
                f"(check scan_naked_positions for protection status)",
            ))
        # else: sums match exactly, nothing to fix.

        _dedupe_stops(adapter, live, uic, symbol, findings)
        for fixed_e in _check_stop_integrity(adapter, live, entries, symbol, findings, removed_keys):
            if fixed_e not in updated:
                updated.append(fixed_e)

    if updated or removed_keys:
        adapter.save(updated, removed_keys)

    return findings


def _entry_stop_price(adapter: BaseAdapter, lp: LocalPosition) -> float:
    """A quantity fix must never silently change the risk level, so this
    reads the CURRENT live trigger price straight off the position's
    existing working stop order (before it gets cancelled) rather than
    inventing a new one. Returns 0.0 if there's no stop_order_id to read
    (e.g. stocks, which don't track one locally at all — replace_stop()
    is a no-op for that adapter so this value is never used there)."""
    if not lp.stop_order_id:
        return 0.0
    try:
        resp = saxo_client.get_orders()
        for o in resp.get("Data", resp):
            if str(o.get("OrderId")) == str(lp.stop_order_id):
                return float(o.get("Price"))
    except Exception:
        pass
    return 0.0


def _dedupe_stops(adapter: BaseAdapter, live: LiveSnapshot, uic: int, symbol: str,
                  findings: list[Finding]) -> None:
    """Cancel exact-duplicate Working stop orders on the same instrument
    (same BuySell + same Price) — leftovers from breakeven moves or
    race-condition retries that never got cancelled. Keeps the newest."""
    stops = live.working_stops(uic)
    groups: dict = {}
    for o in stops:
        price = o.get("Price") or o.get("OrderPrice")
        sig = (o.get("BuySell"), round(float(price), 6) if price is not None else None)
        groups.setdefault(sig, []).append(o)
    for sig, dupes in groups.items():
        if len(dupes) < 2:
            continue
        dupes_sorted = sorted(dupes, key=lambda o: o.get("OrderTime", ""))
        keep = dupes_sorted[-1]
        for o in dupes_sorted[:-1]:
            adapter.cancel_stop(str(o["OrderId"]))
            findings.append(Finding(
                adapter.module, KIND_DUPLICATE_STOP, symbol,
                f"cancelled duplicate {sig[0]} stop {o['OrderId']} @ {sig[1]} "
                f"(kept {keep['OrderId']}, same instrument/side/price)",
            ))


_STOP_PRICE_TOLERANCE_PCT = 0.001   # 0.1% -- rounding/decimal-precision noise, not a real drift


def _check_stop_integrity(adapter: BaseAdapter, live: LiveSnapshot, entries: list[LocalPosition],
                          symbol: str, findings: list[Finding],
                          removed_keys: list[str]) -> list[LocalPosition]:
    """Verify each local entry's remembered stop_order_id is still a real,
    live, correctly-priced Working order -- not just that SOME stop covers
    the instrument (scan_naked_positions already checks that). Two distinct
    failure modes, handled differently:

    - stop_order_id doesn't correspond to any live Working order at all
      (filled, cancelled, or the placement silently never really
      succeeded): unambiguous -- the position needs a real stop and
      local's own stop_price is a trusted, already-computed risk level
      (the strategy's own number, not a guess), so this is fixed
      immediately by placing one there. Reported as KIND_STOP_MISSING.
    - a Working order DOES exist at that id, but its live price differs
      from what local state believes (root cause found 2026-08-24: save()
      never persisted stop_price, only quantity/stop_order_id, so any
      trailing/breakeven update that changed the real broker order left
      local's remembered price stale forever after -- now fixed, but this
      corrects the backlog it already created). The broker's real Working
      order is what actually protects the position and is what any
      future trailing/breakeven update will read live from Saxo before
      moving it further anyway, so it's the trustworthy side here --
      local simply adopts it. Auto-corrected, not just reported.
      KIND_STOP_STALE.

    Replaces the previous dashboard-only "near stop" warning, which
    required someone to be watching the terminal to notice anything --
    this runs on housekeeping's own schedule regardless, and unlike a
    proximity warning it catches a REAL bug (wrong protection level), not
    just "price is close to where it's supposed to stop.\""""
    fixed: list[LocalPosition] = []
    live_orders = {str(o.get("OrderId")): o
                   for o in live.orders_by_uic.get(entries[0].uic, [])}
    for e in entries:
        if e.key in removed_keys or not e.stop_order_id or not e.stop_price:
            continue
        order = live_orders.get(str(e.stop_order_id))
        if order is None or order.get("Status") != "Working":
            new_oid = adapter.replace_stop(e, e.quantity, e.stop_price)
            if new_oid:
                e.stop_order_id = new_oid
                fixed.append(e)
                findings.append(Finding(
                    adapter.module, KIND_STOP_MISSING, symbol,
                    f"{e.key}: remembered stop {e.stop_order_id} was not a live Working "
                    f"order (filled, cancelled, or never really placed) — re-placed a new "
                    f"stop {new_oid} at this entry's own {e.stop_price} risk level",
                ))
            else:
                findings.append(Finding(
                    adapter.module, KIND_STOP_REPLACE_FAILED, symbol,
                    f"{e.key}: remembered stop {e.stop_order_id} was not a live Working "
                    f"order, and re-placing one at {e.stop_price} FAILED — position may be "
                    f"unprotected, check manually",
                ))
            continue

        live_price_raw = order.get("Price")
        if live_price_raw is None:
            continue
        try:
            live_price = float(live_price_raw)
        except (TypeError, ValueError):
            continue
        if abs(live_price - e.stop_price) > max(abs(e.stop_price) * _STOP_PRICE_TOLERANCE_PCT, 1e-6):
            old_price = e.stop_price
            e.stop_price = live_price
            fixed.append(e)
            findings.append(Finding(
                adapter.module, KIND_STOP_STALE, symbol,
                f"{e.key}: local state believed its stop ({e.stop_order_id}) was at "
                f"{old_price}, but the real broker order is at {live_price} — local "
                f"corrected to match the real broker order (the position was always "
                f"protected at {live_price}; only local's bookkeeping was wrong)",
            ))
    return fixed


def reconcile_all(modules: list[str] | None = None, aggressive: bool = False,
                  snapshot: "LiveSnapshot | None" = None, send_email: bool = True) -> list[Finding]:
    """Run reconciliation across the given modules (default: all four)
    against a single fresh Saxo snapshot (or a caller-supplied one — see
    safeguard.py, which shares one snapshot across reconcile_all(),
    scan_naked_positions(), and its own fix pass rather than hitting Saxo's
    API three times). Emails a report only if any finding was produced.
    Safe to call after every live run or on a periodic schedule — a clean
    account produces zero findings and no email."""
    modules = modules or list(ADAPTERS)
    live = snapshot or fetch_live_snapshot()
    all_findings: list[Finding] = []
    for name in modules:
        adapter = ADAPTERS[name]
        try:
            all_findings.extend(reconcile_module(adapter, live, aggressive=aggressive))
        except Exception as exc:
            logger.warning(f"[housekeeping] {name} reconciliation failed: {exc}")
            all_findings.append(Finding(name, "error", "", f"reconciliation crashed: {exc}"))

    try:
        all_findings.extend(_scan_fully_untracked(live, modules))
    except Exception as exc:
        logger.warning(f"[housekeeping] fully-untracked scan failed: {exc}")

    if all_findings:
        for f in all_findings:
            logger.warning(f"[housekeeping] {f.module}/{f.kind} {f.symbol}: {f.detail}")
        if send_email:
            _send_reconcile_email(all_findings)
    else:
        logger.info("[housekeeping] reconcile_all: no mismatches found")
    return all_findings


def _scan_fully_untracked(live: LiveSnapshot, modules: list[str]) -> list[Finding]:
    """Catch a live position that reconcile_module() structurally cannot
    see: one with ZERO local footprint in ANY module, not just an
    imbalance against an existing local record.

    reconcile_module() groups by uic starting from LOCAL entries
    (_group_by_uic(local)) — a uic that never appears in local state at
    all never enters that loop. Found 2026-08-24 via two real incidents
    that both hid in exactly this gap: a fully-untracked 20,000-share
    stock position that went naked then self-closed before anyone
    caught it, and a -2,381,000 EURCHF position (three near-simultaneous
    fills from a pre-cross-process-lock race condition) that sat
    unreconciled for 5 days — invisible to reconcile_all() the whole
    time, and only visible to scan_naked_positions() during the narrow
    windows its stop happened to lapse.

    Report-only, like every other ambiguous finding here: a
    fully-untracked live position could be a genuine bug, or a position
    opened manually/outside any tracked strategy on purpose. Only a
    human decides what it is; this just guarantees it gets surfaced
    instead of silently sitting outside every check that exists."""
    findings: list[Finding] = []
    tracked_uics: dict[str, set[int]] = {}
    for name in modules:
        try:
            tracked_uics[name] = {lp.uic for lp in ADAPTERS[name].load()}
        except Exception:
            tracked_uics[name] = set()
    all_tracked: set[int] = set()
    for uics in tracked_uics.values():
        all_tracked |= uics
    forex_uics = _forex_universe_uics()

    for uic, positions in live.positions_by_uic.items():
        if uic in all_tracked:
            continue
        net = live.net_amount(uic)
        if net == 0:
            continue
        base0 = positions[0]["PositionBase"]
        asset_type = base0.get("AssetType", "")
        module = _ASSET_TYPE_MODULE.get(asset_type)
        if asset_type == "FxSpot":
            module = "forex" if uic in forex_uics else "futures"
        if module not in modules:
            continue  # not one of the modules this run was asked to check
        symbol = _symbol_hint(positions[0], module)
        findings.append(Finding(
            module, KIND_FULLY_UNTRACKED, symbol,
            f"uic {uic}: live net {net:+,.0f} has ZERO local record in any module "
            f"(not even a mismatched one) — reconcile_module() cannot see this uic "
            f"at all; only scan_naked_positions() would ever have flagged it, and "
            f"only while it happens to be unprotected",
            uic=uic, net_amount=net, asset_type=asset_type,
        ))
    return findings


# ── Naked-position scan (read-only, live-Saxo-only) ────────────────────────

@dataclass
class NakedPosition:
    module:         str
    symbol:         str
    uic:            int
    direction:      str
    quantity:       float
    protection:     str        # "none" | "tp_only" | "partial"
    asset_type:     str = ""
    current_price:  float = 0.0
    stop_coverage:  float = 0.0   # quantity already covered by an existing (partial) stop
    uncovered_qty:  float = 0.0   # quantity - stop_coverage; what a fix still needs to protect


_ASSET_TYPE_MODULE = {
    "FxSpot":          None,   # ambiguous (forex AND some futures use it) — resolved via uic lookup below
    "CfdOnIndex":      "futures",
    "ContractFutures": "futures",
    "Etf":             "etf",
    "Stock":           "stocks",
    "CfdOnStock":      "stocks",
}

_forex_universe_uics_cache: set[int] | None = None


def _forex_universe_uics() -> set[int]:
    """Every uic in forex's full configured pair universe (the real count
    grows over time -- forex.universe.PAIRS, not a fixed literal here),
    not just ones a strategy currently happens to hold. Disambiguating an
    untracked FxSpot uic against only CURRENTLY-TRACKED forex positions
    (as this used to do) is wrong by construction for exactly the
    positions this matters for: a fully-untracked uic can never appear
    in forex's local state, so it would always fall through to
    "futures" by default -- mislabeling ordinary forex pairs like
    EURUSD. Found 2026-08-24 building _scan_fully_untracked(), which
    made the same pre-existing mislabeling in scan_naked_positions()
    much more visible (12 of 13 ambiguous uics in one live run turned
    out to be real forex pairs, not futures). Cached for the process
    lifetime — the pair universe doesn't change at runtime."""
    global _forex_universe_uics_cache
    if _forex_universe_uics_cache is None:
        try:
            import forex.runner as fr
            _forex_universe_uics_cache = {p["uic"] for p in fr.PAIRS}
        except Exception:
            _forex_universe_uics_cache = set()
    return _forex_universe_uics_cache


def scan_naked_positions(snapshot: "LiveSnapshot | None" = None,
                         send_email: bool = True) -> list[NakedPosition]:
    """Read-only safety scan across every live Saxo position, regardless
    of which module (or none) is tracking it locally: does a working
    stop-loss order actually cover it?

    Deliberately does NOT auto-close or auto-protect anything it finds.
    Unlike a quantity mismatch (where "make the numbers agree" has one
    obviously-correct direction), a naked position could be:
      - a genuine bug (the class this scan exists to catch), or
      - a strategy that intentionally manages risk without a broker-side
        stop (e.g. a limit-only take-profit design), or
      - a position mid-way through this module's OWN entry sequence,
        caught between the market fill and the stop-order POST.
    Only a human (or the specific strategy's own code, which knows its
    own risk design) should decide whether "no stop" here means "bug" or
    "by design" — this function's job is only to make sure that decision
    actually gets made, via the report/email, not to guess.
    """
    live = snapshot or fetch_live_snapshot()
    naked: list[NakedPosition] = []

    forex_uics = _forex_universe_uics()

    for uic, positions in live.positions_by_uic.items():
        # Aggregate to ONE finding per UIC, not one per position ticket.
        # Saxo doesn't tie a working stop order to a specific ticket — ANY
        # working stop for this uic/side reduces the SAME shared pool of
        # exposure regardless of which ticket it was originally meant for.
        # Checking each ticket independently against the uic's total stop
        # coverage double(or n-times)-credits that same coverage across
        # every ticket sharing it: found 2026-08-24 building safeguard.py's
        # fix pass — a uic with 2+ naked tickets AND some pre-existing
        # partial coverage would get "fixed" with a real gap still left
        # over, because each ticket's own uncovered_qty subtracted the
        # SAME existing coverage instead of it being spent once, and the
        # post-fix verification (also per-ticket) couldn't see the gap
        # either since summed new coverage still cleared each ticket's own
        # amount individually.
        net_amount = live.net_amount(uic)
        if net_amount == 0:
            continue
        base0 = positions[0]["PositionBase"]
        asset_type = base0.get("AssetType", "")
        module = _ASSET_TYPE_MODULE.get(asset_type)
        if asset_type == "FxSpot":
            module = "forex" if uic in forex_uics else "futures"

        direction = "Buy" if net_amount > 0 else "Sell"
        close_side = "Sell" if direction == "Buy" else "Buy"
        stops = [o for o in live.orders_by_uic.get(uic, [])
                 if o.get("Status") == "Working" and o.get("BuySell") == close_side
                 and o.get("OpenOrderType") in _STOP_ORDER_TYPES]
        limits = [o for o in live.orders_by_uic.get(uic, [])
                  if o.get("Status") == "Working" and o.get("BuySell") == close_side
                  and o.get("OpenOrderType") == "Limit"]

        stop_coverage = sum(o["Amount"] for o in stops)
        if stop_coverage >= abs(net_amount):
            continue  # fully protected
        protection = "tp_only" if (not stops and limits) else "partial" if stops else "none"

        symbol = _symbol_hint(positions[0], module)
        prices = [p.get("PositionView", {}).get("CurrentPrice") for p in positions]
        current_price = next((p for p in prices if p), 0.0)
        naked.append(NakedPosition(
            module=module or "unknown", symbol=symbol, uic=uic,
            direction=direction, quantity=abs(net_amount), protection=protection,
            asset_type=asset_type, current_price=float(current_price),
            stop_coverage=stop_coverage, uncovered_qty=abs(net_amount) - stop_coverage,
        ))

    if naked:
        for n in naked:
            logger.warning(f"[housekeeping] NAKED {n.module}/{n.symbol} {n.direction} "
                           f"{n.quantity:,.0f} — protection={n.protection}")
        if send_email:
            _send_naked_email(naked)
    else:
        logger.info("[housekeeping] scan_naked_positions: everything protected")
    return naked


def _symbol_hint(position: dict, module: str | None) -> str:
    npid = position.get("NetPositionId", "")
    if npid.endswith("__FxSpot") or "__" in npid:
        return npid.split("__")[0]
    return npid or str(position["PositionBase"]["Uic"])


NEAR_STOP_THRESHOLD_PCT = 0.5   # matches the removed forex_dashboard.py warning's own threshold


@dataclass
class NearStopPosition:
    module:        str
    symbol:        str
    uic:           int
    direction:     str
    quantity:      float
    current_price: float
    stop_price:    float
    distance_pct:  float


def scan_near_stop_positions(snapshot: "LiveSnapshot | None" = None,
                             send_email: bool = True,
                             threshold_pct: float = NEAR_STOP_THRESHOLD_PCT) -> list[NearStopPosition]:
    """Read-only scan across every live, ALREADY-protected position: is the
    live price within threshold_pct of its own protective stop right now?

    This used to be a forex_dashboard.py-only warning, which meant it only
    ever got noticed if someone happened to have the terminal open at the
    right moment. Moved here (2026-08-24) so it runs on housekeeping's own
    schedule regardless of who's watching. Distinct from
    scan_naked_positions(): that asks "is this protected at all", this
    asks "is protection about to be tested" -- a position with zero stop
    coverage is scan_naked_positions()'s job, not this one, so it's
    skipped here rather than double-reported.

    Report-only, like every other proximity/informational finding here --
    a position near its stop isn't a bug, it's just where price is; the
    real bug class (a stop that's silently WRONG) is
    _check_stop_integrity()'s job, wired into reconcile_module() instead."""
    live = snapshot or fetch_live_snapshot()
    near: list[NearStopPosition] = []
    forex_uics = _forex_universe_uics()

    for uic, positions in live.positions_by_uic.items():
        net_amount = live.net_amount(uic)
        if net_amount == 0:
            continue
        base0 = positions[0]["PositionBase"]
        asset_type = base0.get("AssetType", "")
        module = _ASSET_TYPE_MODULE.get(asset_type)
        if asset_type == "FxSpot":
            module = "forex" if uic in forex_uics else "futures"

        direction  = "Buy" if net_amount > 0 else "Sell"
        close_side = "Sell" if direction == "Buy" else "Buy"
        stops = [o for o in live.orders_by_uic.get(uic, [])
                 if o.get("Status") == "Working" and o.get("BuySell") == close_side
                 and o.get("OpenOrderType") in _STOP_ORDER_TYPES]
        if not stops:
            continue  # unprotected -- scan_naked_positions()'s job, not this one

        prices = [p.get("PositionView", {}).get("CurrentPrice") for p in positions]
        current_price = next((p for p in prices if p), 0.0)
        if not current_price:
            continue

        # Closest stop (by price) to current price is the one that matters --
        # if several strategies each hold their own stop on the same uic,
        # the nearest is what actually determines "about to be tested."
        closest = min(stops, key=lambda o: abs(float(o.get("Price", 0)) - current_price))
        stop_price = float(closest.get("Price", 0))
        if stop_price <= 0:
            continue
        distance_pct = abs(current_price - stop_price) / current_price * 100
        is_near = ((direction == "Buy" and current_price < stop_price * (1 + threshold_pct / 100)) or
                  (direction == "Sell" and current_price > stop_price * (1 - threshold_pct / 100)))
        if not is_near:
            continue

        symbol = _symbol_hint(positions[0], module)
        near.append(NearStopPosition(
            module=module or "unknown", symbol=symbol, uic=uic, direction=direction,
            quantity=abs(net_amount), current_price=float(current_price),
            stop_price=stop_price, distance_pct=distance_pct,
        ))

    if near:
        for n in near:
            logger.warning(f"[housekeeping] NEAR-STOP {n.module}/{n.symbol} {n.direction} "
                           f"{n.quantity:,.0f} — {n.distance_pct:.2f}% from stop ({n.stop_price})")
        if send_email:
            _send_near_stop_email(near, threshold_pct)
    else:
        logger.info("[housekeeping] scan_near_stop_positions: nothing within threshold")
    return near


# ── Email ───────────────────────────────────────────────────────────────

def _load_email_cfg() -> dict | None:
    import json
    if not os.path.exists(_EMAIL_CFG):
        return None
    try:
        with open(_EMAIL_CFG) as f:
            return json.load(f)
    except Exception:
        return None


def _send_email(subject: str, html: str) -> bool:
    cfg = _load_email_cfg()
    if not cfg:
        logger.info(f"[housekeeping] no config/email.json — would have sent: {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Housekeeping Agent <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        return True
    except Exception as exc:
        print(f"  [housekeeping] email FAILED: {exc}", file=sys.stderr)
        return False


def _finding_rows(findings: list[Finding]) -> str:
    rows = ""
    for f in findings:
        badge = "estimate" if f.estimate else f.kind
        rows += (f"<tr><td>{f.module}</td><td>{f.symbol}</td>"
                 f"<td>{f.kind}{' (estimate)' if f.estimate else ''}</td>"
                 f"<td>{f.detail}</td></tr>")
    return rows


def _send_reconcile_email(findings: list[Finding]) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = _finding_rows(findings)
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2>Housekeeping — {len(findings)} state mismatch(es) found and reconciled</h2>
    <p style="color:#666">{now}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Module</th><th>Symbol</th><th>Kind</th><th>Detail</th></tr>
    {rows}
    </table>
    <p style="color:#666;font-size:12px">Local state was corrected to match live Saxo. Any
    "estimate" items involve per-strategy attribution that Saxo's own netting has erased —
    the split preserves relative risk sizing but is not independently verifiable.</p>
    </body></html>"""
    _send_email(f"Housekeeping: {len(findings)} mismatch(es) reconciled — {now}", html)


def _send_naked_email(naked: list[NakedPosition]) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = "".join(
        f"<tr><td>{n.module}</td><td>{n.symbol}</td><td>{n.direction}</td>"
        f"<td>{n.quantity:,.0f}</td><td>{n.protection}</td></tr>"
        for n in naked
    )
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">Naked position alert — {len(naked)} unprotected live position(s)</h2>
    <p style="color:#666">{now}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Module</th><th>Symbol</th><th>Direction</th><th>Quantity</th><th>Protection</th></tr>
    {rows}
    </table>
    <p style="color:#666;font-size:12px">protection: "none" = no stop or TP at all;
    "tp_only" = a take-profit limit exists but no stop-loss; "partial" = a stop exists but
    covers less than the full quantity. This scan does not auto-close or auto-protect —
    review and decide manually.</p>
    </body></html>"""
    _send_email(f"⚠ Naked positions: {len(naked)} unprotected — {now}", html)


def _send_near_stop_email(near: list[NearStopPosition], threshold_pct: float) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = "".join(
        f"<tr><td>{n.module}</td><td>{n.symbol}</td><td>{n.direction}</td>"
        f"<td>{n.quantity:,.0f}</td><td>{n.current_price}</td><td>{n.stop_price}</td>"
        f"<td>{n.distance_pct:.2f}%</td></tr>"
        for n in near
    )
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#d68910">Stop proximity alert — {len(near)} position(s) within {threshold_pct:.1f}% of stop</h2>
    <p style="color:#666">{now}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Module</th><th>Symbol</th><th>Direction</th><th>Quantity</th><th>Price</th><th>Stop</th><th>Distance</th></tr>
    {rows}
    </table>
    <p style="color:#666;font-size:12px">Each of these already has a real, working protective
    stop — this is not a naked-position alert, just a heads-up that price is close to
    triggering it. If a stop fires normally, nothing to do. If you're seeing the SAME
    position here run after run without ever actually closing, that's worth checking --
    see _check_stop_integrity() for the separate check that catches a stop silently sitting
    at the wrong price.</p>
    </body></html>"""
    _send_email(f"⚠ Stop proximity: {len(near)} position(s) within {threshold_pct:.1f}% — {now}", html)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--modules", nargs="*", default=None,
                   help="subset of forex/futures/etf/stocks (default: all)")
    p.add_argument("--naked-only", action="store_true")
    p.add_argument("--reconcile-only", action="store_true")
    args = p.parse_args()

    if not args.naked_only:
        reconcile_all(args.modules)
    if not args.reconcile_only:
        scan_naked_positions()
        scan_near_stop_positions()
