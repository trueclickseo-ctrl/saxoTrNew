"""
housekeeping_live_eur.py
-------------------------
Cross-check / reconciliation for the real-money Saxo LIVE EUR sub-account
ONLY (RSI Pullback on the 83 EXOTIC pairs, see forex/runner.py's
LIVE_EUR_ALLOWED_STRATEGIES). Deliberately a separate file from both
housekeeping.py (SIM) and housekeeping_live.py (the SEK LIVE account) --
same reasoning as housekeeping_live.py's own docstring: this account never
shares SIM's or the SEK account's adapters, entry points, or state.

What IS reused: the same generic, account-agnostic building blocks as
housekeeping_live.py (LocalPosition/Finding/LiveSnapshot, reconcile_module(),
_symbol_hint()) -- none of it carries SIM or SEK-account data.

CRITICAL DIFFERENCE from housekeeping_live.py, found live 2026-08-26 while
building this: Saxo's /port/v1/positions/me and /port/v1/orders/me are
POOLED across all 3 sub-accounts under this Client (SEK/EUR/USD) --
confirmed empirically that passing this account's own AccountKey as an
explicit filter still returns the SEK account's positions unchanged. There
is no way to ask Saxo's API to scope these two endpoints to just this
sub-account. Fetching the raw snapshot naively (like housekeeping_live.py
does for the SEK account, where this was never noticed because nothing else
in the group had ever traded) would make every one of the SEK account's
real, already-tracked-elsewhere positions look "fully untracked" from this
script's point of view -- a false alarm, not a real problem.

Fix: fetch_live_snapshot() filters positions_by_uic/orders_by_uic down to
EXOTIC_SYMBOLS-tier uics only, immediately after the raw fetch, before any
analysis touches it. Since this account can structurally only ever hold
exotic-pair positions (LIVE_EUR_ALLOWED_STRATEGIES={"rsi"} +
_filter_pairs_for_account() restricts entries to EXOTIC_SYMBOLS), scoping
by WHAT this account could ever legitimately hold achieves the same result
the AccountKey filter was supposed to but doesn't -- every check downstream
(reconcile_module, fully-untracked scan, naked scan) then behaves exactly
as if the snapshot were properly account-scoped, without needing any
special-casing in the checks themselves.

Usage:
    python housekeeping_live_eur.py    # reconcile + naked-position scan, once
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
from forex.universe import EXOTIC_SYMBOLS, get_pair

from housekeeping import (
    LocalPosition, Finding, LiveSnapshot, BaseAdapter, reconcile_module,
    KIND_FULLY_UNTRACKED,
    _symbol_hint,
)

logger = logging.getLogger("housekeeping_live_eur")

_ROOT = os.path.dirname(os.path.abspath(__file__))

# uic -> True for every uic that belongs to an EXOTIC pair -- built once
# from forex.universe's own pair list, used to filter the (pooled) raw
# snapshot down to only what this account could ever legitimately hold.
_EXOTIC_UICS = {get_pair(sym)["uic"] for sym in EXOTIC_SYMBOLS}


# ── Snapshot ────────────────────────────────────────────────────────────

def fetch_live_snapshot() -> LiveSnapshot:
    """Always env="live_eur". See module docstring for why this filters to
    EXOTIC_SYMBOLS uics only -- Saxo's positions/orders endpoints are
    pooled across all 3 sub-accounts in this Client, not scoped by
    AccountKey the way balances/me and this file's own account resolution
    might suggest."""
    pos_resp = saxo_client.get_positions(env="live_eur")
    positions = pos_resp.get("Data", pos_resp)
    ord_resp = saxo_client.get_orders(env="live_eur")
    orders = ord_resp.get("Data", ord_resp)

    positions_by_uic: dict = {}
    for p in positions:
        uic = p["PositionBase"]["Uic"]
        if uic not in _EXOTIC_UICS:
            continue
        positions_by_uic.setdefault(uic, []).append(p)

    orders_by_uic: dict = {}
    for o in orders:
        uic = o.get("Uic")
        if uic not in _EXOTIC_UICS:
            continue
        orders_by_uic.setdefault(uic, []).append(o)

    return LiveSnapshot(positions_by_uic, orders_by_uic)


# ── Adapter ─────────────────────────────────────────────────────────────

class ForexLiveEurAdapter(BaseAdapter):
    """Built independently from ForexLiveAdapter (SEK) -- inherits only the
    generic BaseAdapter interface. Reads/writes forex_live_eur_state.json
    via forex.runner.set_account_env("live_eur"), places/cancels real EUR-
    account stop orders via saxo_client(env="live_eur")."""
    module = "forex_live_eur"
    asset_types = {"FxSpot"}

    def _runner(self):
        import forex.runner as r
        r.set_account_env("live_eur")
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
        akey = saxo_client.get_account_key(env="live_eur")
        if pos.stop_order_id:
            self.cancel_stop(pos.stop_order_id)
        dp = r.get_price_decimals(pos.symbol)
        return saxo_order.place_protective_stop(
            post_fn=lambda path, body: saxo_client.post(path, body, env="live_eur"),
            account_key=akey, uic=pos.uic, asset_type="FxSpot",
            amount=new_quantity, direction=pos.direction, stop_price=price,
            label=f"{pos.key} (housekeeping_live_eur)", symbol=pos.symbol, price_decimals=dp,
        )

    def cancel_stop(self, order_id: str) -> bool:
        return saxo_client.cancel_order(order_id, env="live_eur")


# ── Reconciliation ──────────────────────────────────────────────────────

def reconcile_live_eur_forex(aggressive: bool = True, send_email: bool = True,
                             snapshot: "LiveSnapshot | None" = None) -> list[Finding]:
    """The EUR account's only reconciliation entry point. Always uses its
    own (tier-filtered) snapshot/adapter -- never touches housekeeping.
    ADAPTERS, housekeeping.reconcile_all(), or the SEK account's
    housekeeping_live.py equivalents."""
    adapter = ForexLiveEurAdapter()
    live = snapshot or fetch_live_snapshot()
    all_findings: list[Finding] = []
    try:
        all_findings.extend(reconcile_module(adapter, live, aggressive=aggressive))
    except Exception as exc:
        logger.warning(f"[housekeeping_live_eur] forex_live_eur reconciliation failed: {exc}")
        all_findings.append(Finding("forex_live_eur", "error", "", f"reconciliation crashed: {exc}"))

    try:
        all_findings.extend(_scan_fully_untracked(live, adapter))
    except Exception as exc:
        logger.warning(f"[housekeeping_live_eur] fully-untracked scan failed: {exc}")

    if all_findings:
        for f in all_findings:
            logger.warning(f"[housekeeping_live_eur] {f.module}/{f.kind} {f.symbol}: {f.detail}")
        if send_email:
            _send_reconcile_email_live_eur(all_findings)
    else:
        logger.info("[housekeeping_live_eur] reconcile_live_eur_forex: no mismatches found")
    return all_findings


def _scan_fully_untracked(live: LiveSnapshot, adapter: ForexLiveEurAdapter) -> list[Finding]:
    """A live (exotic-tier) position with ZERO local footprint at all --
    own implementation, not SEK's (which assumes CORE-tier uics)."""
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
        symbol = _symbol_hint(positions[0], "forex_live_eur")
        findings.append(Finding(
            "forex_live_eur", KIND_FULLY_UNTRACKED, symbol,
            f"uic {uic}: LIVE EUR net {net:+,.0f} has ZERO local record in the "
            f"forex_live_eur state file at all -- reconcile_module() cannot see "
            f"this uic; only a manual check would catch it.",
        ))
    return findings


# ── Naked-position scan (read-only unless run via safeguard_live_eur.py) ─

@dataclass
class NakedPositionLiveEur:
    symbol:         str
    uic:            int
    direction:      str
    quantity:       float
    protection:     str        # "none" | "tp_only" | "partial"
    current_price:  float = 0.0
    stop_coverage:  float = 0.0
    uncovered_qty:  float = 0.0


_STOP_ORDER_TYPES = ("Stop", "StopLimit", "StopIfTraded", "TrailingStopIfTraded")


def scan_naked_positions_live_eur(snapshot: "LiveSnapshot | None" = None,
                                  send_email: bool = True) -> list[NakedPositionLiveEur]:
    """EUR-account equivalent of housekeeping_live.scan_naked_positions_live().
    Every position here is FxSpot on one of the 83 exotic pairs by
    construction (the snapshot is already tier-filtered) -- read-only,
    see safeguard_live_eur.py for the fix pass."""
    live = snapshot or fetch_live_snapshot()
    naked: list[NakedPositionLiveEur] = []

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
        symbol = _symbol_hint(positions[0], "forex_live_eur")
        prices = [p.get("PositionView", {}).get("CurrentPrice") for p in positions]
        current_price = next((p for p in prices if p), 0.0)
        naked.append(NakedPositionLiveEur(
            symbol=symbol, uic=uic, direction=direction, quantity=abs(net_amount),
            protection=protection, current_price=float(current_price),
            stop_coverage=stop_coverage, uncovered_qty=abs(net_amount) - stop_coverage,
        ))

    if naked:
        for n in naked:
            logger.warning(f"[housekeeping_live_eur] NAKED forex_live_eur/{n.symbol} {n.direction} "
                           f"{n.quantity:,.0f} -- protection={n.protection}")
        if send_email:
            _send_naked_email_live_eur(naked)
    else:
        logger.info("[housekeeping_live_eur] scan_naked_positions_live_eur: everything protected")
    return naked


# ── Email — always [LIVE-EUR]-tagged, own sender ────────────────────────

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


def _send_email_live_eur(subject: str, html: str) -> bool:
    cfg = _load_email_cfg()
    if not cfg:
        logger.info(f"[housekeeping_live_eur] no config/email.json -- would have sent: {subject}")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[LIVE-EUR] {subject}"
        msg["From"]    = f"Housekeeping LIVE-EUR Agent <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        return True
    except Exception as exc:
        logger.warning(f"[housekeeping_live_eur] email FAILED: {exc}")
        return False


def _send_reconcile_email_live_eur(findings: list[Finding]) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = "".join(
        f"<tr><td>{f.module}</td><td>{f.symbol}</td><td>{f.kind}</td><td>{f.detail}</td></tr>"
        for f in findings
    )
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">ATOS LIVE-EUR Housekeeping — {len(findings)} state mismatch(es) found</h2>
    <p style="color:#666">{now} -- real-money EUR sub-account (RSI Pullback / exotic pairs)</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Module</th><th>Symbol</th><th>Kind</th><th>Detail</th></tr>
    {rows}
    </table>
    </body></html>"""
    _send_email_live_eur(f"Housekeeping: {len(findings)} mismatch(es) — {now}", html)


def _send_naked_email_live_eur(naked: list[NakedPositionLiveEur]) -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = "".join(
        f"<tr><td>{n.symbol}</td><td>{n.direction}</td><td>{n.quantity:,.0f}</td>"
        f"<td>{n.protection}</td></tr>"
        for n in naked
    )
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">ATOS LIVE-EUR — {len(naked)} unprotected real-money position(s)</h2>
    <p style="color:#666">{now}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Symbol</th><th>Direction</th><th>Quantity</th><th>Protection</th></tr>
    {rows}
    </table>
    <p style="color:#666;font-size:12px">This scan does not auto-close or auto-protect --
    see safeguard_live_eur.py for the fix pass.</p>
    </body></html>"""
    _send_email_live_eur(f"NAKED real-money position(s): {len(naked)} — {now}", html)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    snap = fetch_live_snapshot()
    reconcile_live_eur_forex(snapshot=snap)
    scan_naked_positions_live_eur(snapshot=snap)
