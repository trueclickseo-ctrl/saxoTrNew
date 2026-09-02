"""
housekeeping_live_stocks.py
---------------------------
Reconciliation + naked-position scan for the real-money US Blend stocks
sleeve (atos_live_stocks.py) ONLY. Separate file from housekeeping.py (SIM,
all modules) and housekeeping_live.py (LIVE forex) -- per the standing
direction that LIVE never shares SIM's adapters / entry points, and each
LIVE sleeve owns its own reconcile path.

Shares only generic, account-agnostic building blocks from housekeeping.py
(the Finding dataclass + KIND_* constants + _symbol_hint) and the
[LIVE]-tagged email sender from housekeeping_live.py. Never imports
housekeeping.ADAPTERS / reconcile_all() / any *Adapter.

Snapshot: Saxo's /positions/me + /orders/me are POOLED across every
sub-account. This keeps ONLY rows where AccountKey == the SEK sub-account
AND AssetType == "Stock" -- the double-filter that makes it safe for this
sleeve and the forex LIVE wind-down to share one Saxo account.

LIVE never auto-closes an untracked position -- it escalates via
attention.raise_attention("live_stocks:..."). A human decides.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

import saxo_client
import saxo_order

from housekeeping import Finding, KIND_FULLY_UNTRACKED, _symbol_hint
from housekeeping_live import _send_email_live

logger = logging.getLogger("housekeeping_live_stocks")
_ROOT = os.path.dirname(os.path.abspath(__file__))

_STOP_ORDER_TYPES = ("Stop", "StopLimit", "StopIfTraded", "TrailingStopIfTraded")
_SECOND_SNAPSHOT_DELAY_S = 3.0
_STOP_CONFIRM_DELAY_S = 2.0


# ── Snapshot ────────────────────────────────────────────────────────────

@dataclass
class StockLiveSnapshot:
    account_key: str
    positions_by_uic: dict          # uic -> [position dicts]
    orders_by_uic: dict             # uic -> [order dicts]

    def net_amount(self, uic) -> float:
        tot = 0.0
        for p in self.positions_by_uic.get(uic, []):
            amt = p.get("PositionBase", {}).get("Amount", 0) or 0
            tot += amt
        return tot


def fetch_live_stock_snapshot() -> StockLiveSnapshot:
    akey = saxo_client.get_account_key(env="live")
    pos = saxo_client.get_positions(env="live")
    orders = saxo_client.get_orders("Stock", env="live")

    p_list = pos.get("Data", pos if isinstance(pos, list) else [])
    o_list = orders.get("Data", orders if isinstance(orders, list) else [])

    positions_by_uic: dict = {}
    for p in p_list:
        pb = p.get("PositionBase", {})
        if pb.get("AccountKey") != akey or pb.get("AssetType") != "Stock":
            continue
        positions_by_uic.setdefault(pb.get("Uic"), []).append(p)

    orders_by_uic: dict = {}
    for o in o_list:
        if o.get("AccountKey") != akey or o.get("AssetType") != "Stock":
            continue
        orders_by_uic.setdefault(o.get("Uic"), []).append(o)

    return StockLiveSnapshot(akey, positions_by_uic, orders_by_uic)


def orders_snapshot_looks_unreliable(snap: StockLiveSnapshot) -> bool:
    """Open stock positions but zero working orders -> a degraded /orders
    fetch, not "every position naked". US Blend attaches an OCO bracket at
    entry, so a funded sleeve always carries working orders."""
    has_pos = any(snap.net_amount(u) for u in snap.positions_by_uic)
    n_orders = sum(len(v) for v in snap.orders_by_uic.values())
    return bool(has_pos) and n_orders == 0


# ── Naked-position scan (read-only) ────────────────────────────────────

@dataclass
class NakedStockLive:
    symbol: str
    uic: int
    direction: str
    quantity: float
    protection: str          # "none" | "tp_only" | "partial"
    current_price: float = 0.0
    stop_coverage: float = 0.0
    uncovered_qty: float = 0.0


def scan_naked_stock_positions(snapshot: StockLiveSnapshot | None = None,
                               send_email: bool = True) -> list[NakedStockLive]:
    live = snapshot or fetch_live_stock_snapshot()
    naked: list[NakedStockLive] = []
    for uic, positions in live.positions_by_uic.items():
        net = live.net_amount(uic)
        if net == 0:
            continue
        direction = "Buy" if net > 0 else "Sell"
        close_side = "Sell" if direction == "Buy" else "Buy"
        working = [o for o in live.orders_by_uic.get(uic, [])
                   if o.get("Status") == "Working" and o.get("BuySell") == close_side]
        stops = [o for o in working if o.get("OpenOrderType") in _STOP_ORDER_TYPES]
        limits = [o for o in working if o.get("OpenOrderType") == "Limit"]
        stop_cov = sum(o.get("Amount", 0) for o in stops)
        if stop_cov >= abs(net):
            continue
        protection = "tp_only" if (not stops and limits) else "partial" if stops else "none"
        symbol = _symbol_hint(positions[0], "stock_live")
        prices = [p.get("PositionView", {}).get("CurrentPrice") for p in positions]
        cur = next((x for x in prices if x), 0.0)
        naked.append(NakedStockLive(
            symbol=symbol, uic=uic, direction=direction, quantity=abs(net),
            protection=protection, current_price=float(cur or 0.0),
            stop_coverage=stop_cov, uncovered_qty=abs(net) - stop_cov,
        ))
    if naked:
        for n in naked:
            logger.warning(f"[hk_live_stocks] NAKED stock_live/{n.symbol} {n.direction} "
                           f"{n.quantity:,.0f} -- protection={n.protection}")
        if send_email:
            _send_naked_email(naked)
    else:
        logger.info("[hk_live_stocks] scan_naked_stock_positions: everything protected")
    return naked


def confirm_naked_stock_live(first_snapshot: StockLiveSnapshot, first_naked: list) -> list:
    """Two-snapshot agreement gate -- never fire a real-money stop off one
    pooled snapshot (mirrors housekeeping_live.confirm_naked_live)."""
    if not first_naked:
        return []
    if orders_snapshot_looks_unreliable(first_snapshot):
        logger.warning("[hk_live_stocks] first snapshot /orders degraded -- skip fix pass")
        return []
    time.sleep(_SECOND_SNAPSHOT_DELAY_S)
    second = fetch_live_stock_snapshot()
    if orders_snapshot_looks_unreliable(second):
        logger.warning("[hk_live_stocks] confirming snapshot /orders degraded -- skip fix pass")
        return []
    second_naked = {n.uic: n for n in scan_naked_stock_positions(snapshot=second, send_email=False)}
    confirmed, dropped = [], []
    for n in first_naked:
        (confirmed.append(second_naked[n.uic]) if n.uic in second_naked
         else dropped.append(n.symbol))
    if dropped:
        logger.warning(f"[hk_live_stocks] {dropped} naked in one snapshot only -- NOT acting")
    return confirmed


def stop_order_is_working(oid: str) -> bool:
    if not oid:
        return False
    try:
        time.sleep(_STOP_CONFIRM_DELAY_S)
        resp = saxo_client.get_orders("Stock", env="live")
        for o in resp.get("Data", resp) or []:
            if str(o.get("OrderId")) == str(oid):
                return o.get("Status") in ("Working", "Parked", "WaitCondition", None)
        return False
    except Exception as exc:
        logger.warning(f"[hk_live_stocks] could not confirm stop {oid}: {exc}")
        return True   # fail open -- next re-scan is the backstop


# ── Reconcile local ledger vs the LIVE account ────────────────────────

def reconcile_live_stocks(send_email: bool = True,
                          snapshot: StockLiveSnapshot | None = None) -> list[Finding]:
    """Compare data/atos_live_stocks.db open rows against the LIVE Saxo
    account. LIVE never auto-closes -- every mismatch is reported / escalated,
    never silently resolved."""
    live = snapshot or fetch_live_stock_snapshot()
    findings: list[Finding] = []

    try:
        from atos import database as _db
        _db.init_db()
        open_rows = [t for t in _db.get_open_trades() if t.get("strategy") == "US Blend"]
    except Exception as exc:
        logger.warning(f"[hk_live_stocks] ledger read failed: {exc}")
        open_rows = []

    try:
        from instrument_map import load_instrument_map, MAP_FILE_LIVE
        imap = load_instrument_map(path=MAP_FILE_LIVE, require_usd=True)
    except Exception:
        imap = {}
    uic_for = {tk: v["uic"] for tk, v in imap.items()}

    tracked_uics = set()
    for r in open_rows:
        u = uic_for.get(r.get("ticker"))
        if u is not None:
            tracked_uics.add(u)
        if u is None or live.net_amount(u) == 0:
            findings.append(Finding(
                "stock_live", "untracked_local", r.get("ticker", "?"),
                f"ledger row (id {r.get('id')}) open but the LIVE account holds no "
                f"matching stock position -- NOT auto-closed, human review",
            ))

    for uic, positions in live.positions_by_uic.items():
        if live.net_amount(uic) == 0 or uic in tracked_uics:
            continue
        sym = _symbol_hint(positions[0], "stock_live")
        findings.append(Finding(
            "stock_live", KIND_FULLY_UNTRACKED, sym,
            f"uic {uic}: LIVE net {live.net_amount(uic):+,.0f} shares with ZERO local "
            f"ledger record -- LIVE never auto-closes; a human must attribute this.",
        ))

    if findings:
        for f in findings:
            logger.warning(f"[hk_live_stocks] {f.kind} {f.symbol}: {f.detail}")
        if send_email:
            _send_reconcile_email(findings)
    else:
        logger.info("[hk_live_stocks] reconcile_live_stocks: clean")
    return findings


# ── Email ─────────────────────────────────────────────────────────────

def _send_reconcile_email(findings: list[Finding]) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = "".join(f"<tr><td>{f.symbol}</td><td>{f.kind}</td><td>{f.detail}</td></tr>"
                   for f in findings)
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">ATOS LIVE STOCKS Housekeeping — {len(findings)} mismatch(es)</h2>
    <p style="color:#666">{now} — real-money US Blend sleeve</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Symbol</th><th>Kind</th><th>Detail</th></tr>{rows}</table>
    </body></html>"""
    _send_email_live(f"STOCKS Housekeeping: {len(findings)} mismatch(es) — {now}", html)


def _send_naked_email(naked: list[NakedStockLive]) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = "".join(f"<tr><td>{n.symbol}</td><td>{n.direction}</td><td>{n.quantity:,.0f}</td>"
                   f"<td>{n.protection}</td></tr>" for n in naked)
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">ATOS LIVE STOCKS — {len(naked)} unprotected real-money position(s)</h2>
    <p style="color:#666">{now}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Symbol</th><th>Direction</th><th>Shares</th><th>Protection</th></tr>{rows}</table>
    <p style="color:#666;font-size:12px">See safeguard_live_stocks.py for the fix pass.</p>
    </body></html>"""
    _send_email_live(f"NAKED real-money stock(s): {len(naked)} — {now}", html)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    snap = fetch_live_stock_snapshot()
    reconcile_live_stocks(snapshot=snap)
    scan_naked_stock_positions(snapshot=snap)
