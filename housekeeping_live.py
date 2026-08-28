"""
housekeeping_live.py
---------------------
Cross-check / reconciliation for the real-money Saxo LIVE forex account
ONLY. Deliberately a separate file from housekeeping.py (SIM's equivalent,
covering forex/futures/etf/stocks), not a function or class living inside
it — per explicit user direction: the LIVE account never shares SIM's
adapters, entry points, or the SIM-only ADAPTERS dict / reconcile_all().

What IS reused from housekeeping.py: purely generic, account-agnostic
building blocks -- the LocalPosition/Finding/LiveSnapshot dataclasses, the
Finding KIND_* constants, the reconcile_module() diffing ALGORITHM itself
(a pure function: give it an adapter + a snapshot, it tells you what
doesn't match -- it has no SIM-specific behavior baked in), and
_symbol_hint(). None of these carry SIM data or SIM account state; they're
the same kind of reuse as importing a stdlib module.

What is NOT reused, ever: housekeeping.ADAPTERS (the SIM module dict),
housekeeping.reconcile_all()/scan_naked_positions() (SIM-scoped entry
points), housekeeping.fetch_live_snapshot() (fetches SIM's account).
This file fetches its own snapshot, always via env="live".

Why this exists at all: LIVE has placed zero real trades so far (as of
2026-08-25), so there is no naked-position risk YET -- but any of the
9 daily entry scans could place a real order starting today, and SIM's
own history (19 naked positions found in one day before safeguard.py
existed) is exactly the failure mode this guards against before it
becomes an incident, not after.

Usage:
    python housekeeping_live.py              # reconcile + naked-position scan, once
"""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import saxo_client
import saxo_order

from housekeeping import (
    LocalPosition, Finding, LiveSnapshot, BaseAdapter, reconcile_module,
    KIND_FULLY_UNTRACKED,
    _symbol_hint,
)

logger = logging.getLogger("housekeeping_live")

_ROOT = os.path.dirname(os.path.abspath(__file__))


# ── Snapshot ────────────────────────────────────────────────────────────

def fetch_live_snapshot() -> LiveSnapshot:
    """Always env="live" -- this file has no other mode. Never shares a
    snapshot with SIM's own fetch_live_snapshot() in housekeeping.py.

    2026-08-26 through 2026-08-27: filtered by pair-tier (CORE_SYMBOLS, then
    narrowed to HIGH_VOLUME_SYMBOLS) as a workaround, on the belief that
    Saxo's pooled /port/v1/positions/me and /port/v1/orders/me endpoints
    carried no per-record account attribution at all (confirmed only that
    passing AccountKey as a QUERY PARAM doesn't filter server-side -- true,
    but a different claim). That belief was wrong: verified live 2026-08-28
    that every position (PositionBase.AccountKey) and order (AccountKey,
    top-level) already carries its own AccountKey, even though the pooled
    endpoint returns all 3 sub-accounts' records together. This filters by
    THAT field directly -- the real, broker-verified attribution -- instead
    of inferring ownership from which pairs an account is "supposed to"
    trade. This is what makes it safe for this account (bb) and the EUR
    account (rsi, housekeeping_live_eur.py) to trade the SAME 17-pair
    HIGH_VOLUME_SYMBOLS universe (explicit user decision, 2026-08-28) --
    pair-tier partitioning is no longer load-bearing for correctness."""
    akey = saxo_client.get_account_key(env="live")

    pos_resp = saxo_client.get_positions(env="live")
    positions = pos_resp.get("Data", pos_resp)
    ord_resp = saxo_client.get_orders(env="live")
    orders = ord_resp.get("Data", ord_resp)

    positions_by_uic: dict = {}
    for p in positions:
        if p["PositionBase"].get("AccountKey") != akey:
            continue
        uic = p["PositionBase"]["Uic"]
        positions_by_uic.setdefault(uic, []).append(p)

    orders_by_uic: dict = {}
    for o in orders:
        if o.get("AccountKey") != akey:
            continue
        uic = o.get("Uic")
        orders_by_uic.setdefault(uic, []).append(o)

    return LiveSnapshot(positions_by_uic, orders_by_uic)


# ── Adapter ─────────────────────────────────────────────────────────────

class ForexLiveAdapter(BaseAdapter):
    """Built independently from housekeeping.py's ForexAdapter (SIM) --
    inherits only the generic BaseAdapter interface, not SIM's
    implementation. Reads/writes forex_live_state.json via
    forex.runner.set_account_env("live"), places/cancels real LIVE stop
    orders via saxo_client(env="live") + saxo_order.py (the generic,
    already env-aware bracket-order library -- reusing it is not "using
    SIM", it takes whichever account_key/post_fn it's given)."""
    module = "forex_live"
    asset_types = {"FxSpot"}

    def _runner(self):
        import forex.runner as r
        r.set_account_env("live")
        return r

    def load(self) -> list[LocalPosition]:
        r = self._runner()
        state = r._load_state()
        out = []
        for key, v in state.get("positions", {}).items():
            symbol = key.split(":", 1)[1] if ":" in key else key
            out.append(LocalPosition(
                module=self.module, key=key, uic=v["uic"], symbol=symbol,
                direction=v.get("direction", "Buy"), quantity=int(v["quantity"]),
                asset_type="FxSpot", stop_order_id=v.get("stop_order_id"),
                stop_price=float(v.get("stop_price") or 0.0),
            ))
        return out

    def save(self, positions: list[LocalPosition], removed_keys: list[str]) -> None:
        r = self._runner()
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
        r = self._runner()
        akey = saxo_client.get_account_key(env="live")
        if pos.stop_order_id:
            self.cancel_stop(pos.stop_order_id)
        dp = r.get_price_decimals(pos.symbol)
        return saxo_order.place_protective_stop(
            post_fn=lambda path, body: saxo_client.post(path, body, env="live"),
            account_key=akey, uic=pos.uic, asset_type="FxSpot",
            amount=new_quantity, direction=pos.direction, stop_price=price,
            label=f"{pos.key} (housekeeping_live)", symbol=pos.symbol, price_decimals=dp,
        )

    def cancel_stop(self, order_id: str) -> bool:
        return saxo_client.cancel_order(order_id, env="live")


# ── Reconciliation ──────────────────────────────────────────────────────

def reconcile_live_forex(aggressive: bool = True, send_email: bool = True,
                         snapshot: "LiveSnapshot | None" = None) -> list[Finding]:
    """The LIVE account's only reconciliation entry point. Always uses its
    own snapshot/adapter -- never touches housekeeping.ADAPTERS or
    reconcile_all()."""
    adapter = ForexLiveAdapter()
    live = snapshot or fetch_live_snapshot()
    all_findings: list[Finding] = []
    try:
        all_findings.extend(reconcile_module(adapter, live, aggressive=aggressive))
    except Exception as exc:
        logger.warning(f"[housekeeping_live] forex_live reconciliation failed: {exc}")
        all_findings.append(Finding("forex_live", "error", "", f"reconciliation crashed: {exc}"))

    try:
        all_findings.extend(_scan_fully_untracked(live, adapter))
    except Exception as exc:
        logger.warning(f"[housekeeping_live] fully-untracked scan failed: {exc}")

    if all_findings:
        for f in all_findings:
            logger.warning(f"[housekeeping_live] {f.module}/{f.kind} {f.symbol}: {f.detail}")
        if send_email:
            _send_reconcile_email_live(all_findings)
    else:
        logger.info("[housekeeping_live] reconcile_live_forex: no mismatches found")
    return all_findings


def _scan_fully_untracked(live: LiveSnapshot, adapter: ForexLiveAdapter) -> list[Finding]:
    """A live position with ZERO local footprint at all -- reconcile_module()
    groups by uic starting from LOCAL entries, so a uic that never appears
    locally never enters that loop. Own implementation (not SIM's
    _scan_fully_untracked(), which assumes SIM's module set)."""
    try:
        tracked_uics = {lp.uic for lp in adapter.load()}
    except Exception:
        tracked_uics = set()
    findings: list[Finding] = []
    for uic, positions in live.positions_by_uic.items():
        if positions[0]["PositionBase"].get("AssetType") != "FxSpot":
            continue
        if uic in tracked_uics:
            continue
        net = live.net_amount(uic)
        if net == 0:
            continue
        symbol = _symbol_hint(positions[0], "forex_live")
        findings.append(Finding(
            "forex_live", KIND_FULLY_UNTRACKED, symbol,
            f"uic {uic}: LIVE net {net:+,.0f} has ZERO local record in the "
            f"forex_live state file at all -- reconcile_module() cannot see "
            f"this uic; only a manual check would catch it.",
        ))
    return findings


# ── Naked-position scan (read-only unless run via safeguard_live.py) ────

@dataclass
class NakedPositionLive:
    symbol:         str
    uic:            int
    direction:      str
    quantity:       float
    protection:     str        # "none" | "tp_only" | "partial"
    current_price:  float = 0.0
    stop_coverage:  float = 0.0
    uncovered_qty:  float = 0.0


_STOP_ORDER_TYPES = ("Stop", "StopLimit", "StopIfTraded", "TrailingStopIfTraded")


def scan_naked_positions_live(snapshot: "LiveSnapshot | None" = None,
                              send_email: bool = True) -> list[NakedPositionLive]:
    """LIVE-only equivalent of housekeeping.scan_naked_positions(). Simpler
    than SIM's version by construction: every LIVE position is FxSpot on
    one of the 34 core pairs, so there's no forex-vs-futures-vs-etf-vs-
    stocks ambiguity to resolve at all. Read-only -- does not auto-protect
    anything; see safeguard_live.py for the fix pass."""
    live = snapshot or fetch_live_snapshot()
    naked: list[NakedPositionLive] = []

    for uic, positions in live.positions_by_uic.items():
        if positions[0]["PositionBase"].get("AssetType") != "FxSpot":
            continue
        net_amount = live.net_amount(uic)
        if net_amount == 0:
            continue
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
        symbol = _symbol_hint(positions[0], "forex_live")
        prices = [p.get("PositionView", {}).get("CurrentPrice") for p in positions]
        current_price = next((p for p in prices if p), 0.0)
        naked.append(NakedPositionLive(
            symbol=symbol, uic=uic, direction=direction, quantity=abs(net_amount),
            protection=protection, current_price=float(current_price),
            stop_coverage=stop_coverage, uncovered_qty=abs(net_amount) - stop_coverage,
        ))

    if naked:
        for n in naked:
            logger.warning(f"[housekeeping_live] NAKED forex_live/{n.symbol} {n.direction} "
                           f"{n.quantity:,.0f} -- protection={n.protection}")
        if send_email:
            _send_naked_email_live(naked)
    else:
        logger.info("[housekeeping_live] scan_naked_positions_live: everything protected")
    return naked


# ── Email — always [LIVE]-tagged, own sender (not housekeeping.py's) ────

def _load_email_cfg():
    import json
    cfg_path = os.path.join(_ROOT, "config", "email.json")
    if not os.path.exists(cfg_path):
        return None
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _send_email_live(subject: str, html: str) -> bool:
    cfg = _load_email_cfg()
    if not cfg:
        logger.info(f"[housekeeping_live] no config/email.json -- would have sent: {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[LIVE] {subject}"
        msg["From"]    = f"Housekeeping LIVE Agent <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        return True
    except Exception as exc:
        logger.warning(f"[housekeeping_live] email FAILED: {exc}")
        return False


def _send_reconcile_email_live(findings: list[Finding]) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = "".join(
        f"<tr><td>{f.module}</td><td>{f.symbol}</td><td>{f.kind}</td><td>{f.detail}</td></tr>"
        for f in findings
    )
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">ATOS LIVE Housekeeping — {len(findings)} state mismatch(es) found</h2>
    <p style="color:#666">{now} -- real-money account</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Module</th><th>Symbol</th><th>Kind</th><th>Detail</th></tr>
    {rows}
    </table>
    </body></html>"""
    _send_email_live(f"Housekeeping: {len(findings)} mismatch(es) — {now}", html)


def _send_naked_email_live(naked: list[NakedPositionLive]) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = "".join(
        f"<tr><td>{n.symbol}</td><td>{n.direction}</td><td>{n.quantity:,.0f}</td>"
        f"<td>{n.protection}</td></tr>"
        for n in naked
    )
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">ATOS LIVE — {len(naked)} unprotected real-money position(s)</h2>
    <p style="color:#666">{now}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Symbol</th><th>Direction</th><th>Quantity</th><th>Protection</th></tr>
    {rows}
    </table>
    <p style="color:#666;font-size:12px">This scan does not auto-close or auto-protect --
    see safeguard_live.py for the fix pass.</p>
    </body></html>"""
    _send_email_live(f"NAKED real-money position(s): {len(naked)} — {now}", html)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    snap = fetch_live_snapshot()
    reconcile_live_forex(snapshot=snap)
    scan_naked_positions_live(snapshot=snap)
