"""
safeguard_live.py
-------------------
Auto-fix agent for the real-money Saxo LIVE forex account ONLY. Runs after
housekeeping_live.py's reconcile_live_forex() + scan_naked_positions_live()
and ACTS on what they find, instead of only reporting it -- same relationship
SIM's safeguard.py has to housekeeping.py, but a fully separate file, not a
shared function/class. Per explicit user direction: LIVE never shares SIM's
adapters, entry points, or auto-fix logic, even though the underlying
CONCEPT (place a conservative protective stop on a naked position, verify
after) is intentionally the same proven design.

Built 2026-08-25, BEFORE any real trade has happened on this account --
deliberately proactive rather than reactive. SIM's safeguard.py was built
reactively, after a live run surfaced 23 unprotected positions and 8 state
mismatches in one day. LIVE's 9x/day schedule means a real entry could
happen at any of the next scheduled runs; the point of building this now
is to have the safety net in place BEFORE that, not after an incident.

ONE real category of automated action here (same as SIM's safeguard.py):

  NAKED / UNDER-PROTECTED LIVE POSITIONS (housekeeping_live.scan_naked_positions_live)
  Places a protective stop for whatever quantity isn't already covered,
  using a conservative default distance from the position's OWN current
  price. Last-resort safety net, NOT a replacement for the originating
  strategy's own (unknown/lost) risk logic. An existing take-profit order
  is left untouched -- only the missing stop-loss leg gets added.

State mismatches (direction_mismatch, untracked_live, fully_untracked) are
resolved the same way reconcile_live_forex(aggressive=True) already
resolves them -- no separate mismatch-fixing pass needed here since the
LIVE account has no multi-module attribution ambiguity SIM's mismatch-
fixing exists to handle (this file only ever deals with forex_live).

VERIFICATION: after every fix, re-fetches a fresh LIVE snapshot and
re-checks that the specific thing just fixed is actually fixed -- never
marks something "fixed" on faith. Sends exactly one [LIVE]-tagged
confirmation email per run, only if there was something to do.

Usage:
    python safeguard_live.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import attention
import saxo_client
import saxo_order

import housekeeping_live as hk_live

logger = logging.getLogger("safeguard_live")

# Conservative last-resort stop distance -- NOT tuned to any strategy's
# real risk logic (unknown/lost for an untracked position), just wide
# enough to avoid an immediate re-trigger on normal noise. Every LIVE
# position is FxSpot by construction (34 core pairs only), so this is a
# single value, not SIM's per-asset-type table.
DEFAULT_STOP_PCT_FXSPOT = 0.02


@dataclass
class FixOutcomeLive:
    symbol:   str
    action:   str
    fixed:    bool
    detail:   str
    uic:      int = 0
    needs_human: bool = False   # LIVE never auto-closes an unattributable position -- a human decides


def _fix_naked_position_live(n: "hk_live.NakedPositionLive") -> FixOutcomeLive:
    if n.uncovered_qty <= 0:
        return FixOutcomeLive(n.symbol, "none needed", True,
                              "already fully covered by the time this ran", uic=n.uic)
    if not n.current_price:
        return FixOutcomeLive(n.symbol, "skipped", False,
                              "no live current price available to base a stop on", uic=n.uic)

    pct = DEFAULT_STOP_PCT_FXSPOT
    if n.direction == "Buy":
        stop_price = n.current_price * (1 - pct)
    else:
        stop_price = n.current_price * (1 + pct)

    akey = saxo_client.get_account_key(env="live")
    import forex.runner as fr
    fr.set_account_env("live")
    dp = fr.get_price_decimals(n.symbol)

    new_oid = saxo_order.place_protective_stop(
        post_fn=lambda path, body: saxo_client.post(path, body, env="live"),
        account_key=akey, uic=n.uic, asset_type="FxSpot",
        amount=int(n.uncovered_qty), direction=n.direction, stop_price=stop_price,
        label=f"safeguard_live:{n.symbol}", symbol=n.symbol, price_decimals=dp,
    )
    if new_oid is None:
        return FixOutcomeLive(n.symbol, "place_protective_stop", False,
                              f"Saxo rejected the protective stop order "
                              f"({n.uncovered_qty:,.0f} @ ~{stop_price:.5f})", uic=n.uic)
    # Saxo returns an OrderId on ACCEPTANCE -- confirm it actually reached
    # the order book before calling this a fix (else it's an unprotected
    # position wearing a fake "fixed" tag until the next run).
    if not hk_live.stop_order_is_working(new_oid):
        saxo_client.cancel_order(new_oid, env="live")
        return FixOutcomeLive(n.symbol, "place_protective_stop", False,
                              f"Saxo returned order id {new_oid} but it never became a live "
                              f"order (rejected post-accept) -- position still unprotected, "
                              f"retrying next cycle", uic=n.uic)
    return FixOutcomeLive(n.symbol, "place_protective_stop", True,
                          f"placed stop for {n.uncovered_qty:,.0f} @ ~{stop_price:.5f} "
                          f"({pct:.0%} from current price {n.current_price:.5f})", uic=n.uic)


def run_safeguard_live() -> list[FixOutcomeLive]:
    """Fetch one LIVE Saxo snapshot, fix every naked position it can, plus
    whatever reconcile_live_forex(aggressive=True) already resolves, then
    re-fetch and verify before reporting."""
    snapshot = hk_live.fetch_live_snapshot()
    outcomes: list[FixOutcomeLive] = []

    first_naked = hk_live.scan_naked_positions_live(snapshot=snapshot, send_email=False)
    # Never fire a real-money stop off a single snapshot -- require the same
    # uic to look naked in a second snapshot ~3s later, and bail entirely if
    # either snapshot's /orders came back degraded (2026-09-02 EURUSD false
    # page). scan_naked_positions_live() still logs/emails the warning above.
    naked = hk_live.confirm_naked_live(snapshot, first_naked)
    for n in naked:
        outcomes.append(_fix_naked_position_live(n))

    mismatch_findings = hk_live.reconcile_live_forex(aggressive=True, send_email=False,
                                                     snapshot=snapshot)
    for f in mismatch_findings:
        if f.kind == "fully_untracked":
            # real money -- ATOS never auto-closes a position it can't
            # attribute to a strategy. Escalate to the human-decision
            # channel (attention.py) instead of claiming "FIXED".
            outcomes.append(FixOutcomeLive(f.symbol, "needs_human_review", False,
                                           f.detail + " — LIVE: not auto-closed. A human must "
                                           f"decide what this position is.",
                                           uic=getattr(f, "uic", 0), needs_human=True))
        elif f.kind == "suspect_orphan":
            # live shows 0 net but a stop order is still "Working" -- reconcile
            # (correctly, after the 2026-08-26 ZC false-positive) refuses to
            # remove it on one snapshot. But if it STAYS suspect for hours
            # (attention's 2h grace), the position really did close and a
            # stop order is dangling -- a human should close the loop
            # (2026-09-01: a SEK donchian:GBPUSD stop-out sat "suspect" ~3.5h).
            outcomes.append(FixOutcomeLive(f.symbol, "suspect_orphan_persisting", False,
                                           f.detail, needs_human=True))
        else:
            outcomes.append(FixOutcomeLive(f.symbol, f.kind, f.kind != "stop_replace_failed",
                                           f.detail))

    if not outcomes:
        logger.info("[safeguard_live] nothing to fix")
        return outcomes

    # ── Verify: re-fetch fresh and re-check what we just touched ──────────
    fresh = hk_live.fetch_live_snapshot()
    if hk_live.orders_snapshot_looks_unreliable(fresh):
        # A degraded verify snapshot would flip every just-placed stop to
        # "VERIFICATION FAILED" on noise -- exactly the false page we're
        # fixing. The placement-time confirm (stop_order_is_working) and the
        # next run's re-scan are the backstops.
        logger.warning("[safeguard_live] verify snapshot's /orders looks degraded -- "
                       "keeping fix outcomes as reported, next run will re-check")
    else:
        still_naked_uics = {n.uic for n in hk_live.scan_naked_positions_live(snapshot=fresh, send_email=False)}
        for o in outcomes:
            if o.action == "place_protective_stop" and o.fixed and o.uic in still_naked_uics:
                o.fixed = False
                o.detail += " -- VERIFICATION FAILED: still shows naked/under-protected after the fix"

    for o in outcomes:
        _tag = "NEEDS HUMAN" if o.needs_human else ("FIXED" if o.fixed else "NOT FIXED")
        logger.info(f"[safeguard_live] {_tag} {o.symbol} ({o.action}): {o.detail}")

    _escalate_live(outcomes)
    _send_safeguard_email_live(outcomes)
    return outcomes


def _escalate_live(outcomes: list[FixOutcomeLive]) -> None:
    """Route LIVE outcomes into the one 'ATOS needs a human' channel:
    an unattributable position, or a fix that keeps failing, escalates to
    one email + a daily nag until it clears. A resolved item clears its
    alert. Then flush the consolidated digest."""
    try:
        for o in outcomes:
            key = f"safeguard-live:{o.symbol}:{o.action}"
            if o.action == "suspect_orphan_persisting":
                attention.raise_attention(
                    key, source="safeguard (LIVE forex)",
                    title=f"{o.symbol}: position looks closed but a stop order is dangling",
                    detail=o.detail + " — if this has been showing for 2h+ the position "
                                      "really did close; cancel the leftover stop / confirm flat "
                                      "in SaxoTrader.",
                    severity="warn", grace_minutes=120, recheck_minutes=180)
            elif o.needs_human:
                attention.raise_attention(
                    key, source="safeguard (LIVE forex)",
                    title=f"{o.symbol}: unattributable real-money position",
                    detail=o.detail, severity="critical",
                    grace_minutes=0, recheck_minutes=180)   # LIVE -> escalate immediately
            elif o.fixed:
                attention.clear_attention(key, note=o.detail[:200])
            else:
                # 90-min grace: a genuine unprotected real-money position
                # that safeguard can't fix for 90 min+ absolutely needs a
                # human; a one-run artifact (e.g. a cross-account position
                # this agent shouldn't have touched, or a mid-fill snapshot)
                # clears before it pages.
                attention.raise_attention(
                    key, source="safeguard (LIVE forex)",
                    title=f"{o.symbol}: {o.action} keeps failing on real money",
                    detail=o.detail, severity="critical",
                    grace_minutes=90, recheck_minutes=180)
        attention.flush()
    except Exception as exc:
        logger.warning(f"[safeguard_live] attention routing failed: {exc}")


def _send_safeguard_email_live(outcomes: list[FixOutcomeLive]) -> None:
    fixed_n = sum(1 for o in outcomes if o.fixed)
    total_n = len(outcomes)
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = ""
    for o in sorted(outcomes, key=lambda o: (not o.fixed, o.symbol)):
        status = "FIXED" if o.fixed else "NOT FIXED"
        color = "#2ea043" if o.fixed else "#da3633"
        rows += (f'<tr><td style="color:{color};font-weight:bold">{status}</td>'
                 f"<td>{o.symbol}</td><td>{o.action}</td><td>{o.detail}</td></tr>")

    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">ATOS LIVE Safeguard: {fixed_n}/{total_n} issue(s) fixed and verified</h2>
    <p style="color:#666">{now} -- real-money account</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Status</th><th>Symbol</th><th>Action</th><th>Detail</th></tr>
    {rows}
    </table>
    <p style="color:#666;font-size:12px">Every "FIXED" row was re-verified against a fresh Saxo
    snapshot after the fix. Naked-position stops use a conservative {DEFAULT_STOP_PCT_FXSPOT:.0%}
    default distance, not the originating strategy's own risk logic (unknown for an untracked
    position) -- see this file's module docstring.</p>
    </body></html>"""
    hk_live._send_email_live(f"Safeguard: {fixed_n}/{total_n} fixed and verified — {now}", html)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    run_safeguard_live()
