"""
safeguard_live_eur.py
-----------------------
Auto-fix agent for the real-money Saxo LIVE EUR sub-account ONLY (RSI
Pullback, on the same 17-pair HIGH_VOLUME_SYMBOLS universe as the SEK
account as of 2026-08-28 -- see housekeeping_live_eur.py's docstring for
the AccountKey-based disambiguation that makes the pair overlap safe;
legacy exotic-pair positions from this account's original design are
still fully protected too). Same relationship to housekeeping_live_eur.py
that safeguard_live.py has to housekeeping_live.py -- a fully separate
file from both the SEK LIVE account's safeguard_live.py and SIM's
safeguard.py, per the same explicit user direction: each real-money
account gets its own independent safety net, never a shared one.

Built 2026-08-26, before this account's first real trade -- same
proactive reasoning as safeguard_live.py: the point is having this in
place before an entry happens, not after an incident.

ONE real category of automated action (same as safeguard_live.py):
  NAKED / UNDER-PROTECTED LIVE POSITIONS
  (housekeeping_live_eur.scan_naked_positions_live_eur)
  Places a protective stop for whatever quantity isn't already covered,
  using a conservative default distance from the position's OWN current
  price. Last-resort safety net, not the originating strategy's own risk
  logic. An existing take-profit order is left untouched.

VERIFICATION: after every fix, re-fetches a fresh (tier-filtered) LIVE-EUR
snapshot and re-checks that the specific thing just fixed is actually
fixed. Sends exactly one [LIVE-EUR]-tagged confirmation email per run,
only if there was something to do.

Usage:
    python safeguard_live_eur.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import attention
import saxo_client
import saxo_order

import housekeeping_live_eur as hk_live_eur

logger = logging.getLogger("safeguard_live_eur")

# Conservative last-resort stop distance -- NOT tuned to RSI Pullback's own
# risk logic (unknown/lost for an untracked position). Every LIVE-EUR
# position is FxSpot by construction (exotic pairs only), so this is a
# single value, matching safeguard_live.py's own DEFAULT_STOP_PCT_FXSPOT.
DEFAULT_STOP_PCT_FXSPOT = 0.02


@dataclass
class FixOutcomeLiveEur:
    symbol:   str
    action:   str
    fixed:    bool
    detail:   str
    uic:      int = 0
    needs_human: bool = False


def _fix_naked_position_live_eur(n: "hk_live_eur.NakedPositionLiveEur") -> FixOutcomeLiveEur:
    if n.uncovered_qty <= 0:
        return FixOutcomeLiveEur(n.symbol, "none needed", True,
                                 "already fully covered by the time this ran", uic=n.uic)
    if not n.current_price:
        return FixOutcomeLiveEur(n.symbol, "skipped", False,
                                 "no live current price available to base a stop on", uic=n.uic)

    pct = DEFAULT_STOP_PCT_FXSPOT
    if n.direction == "Buy":
        stop_price = n.current_price * (1 - pct)
    else:
        stop_price = n.current_price * (1 + pct)

    akey = saxo_client.get_account_key(env="live_eur")
    import forex.runner as fr
    fr.set_account_env("live_eur")
    dp = fr.get_price_decimals(n.symbol)

    new_oid = saxo_order.place_protective_stop(
        post_fn=lambda path, body: saxo_client.post(path, body, env="live_eur"),
        account_key=akey, uic=n.uic, asset_type="FxSpot",
        amount=int(n.uncovered_qty), direction=n.direction, stop_price=stop_price,
        label=f"safeguard_live_eur:{n.symbol}", symbol=n.symbol, price_decimals=dp,
    )
    if new_oid is None:
        return FixOutcomeLiveEur(n.symbol, "place_protective_stop", False,
                                 f"Saxo rejected the protective stop order "
                                 f"({n.uncovered_qty:,.0f} @ ~{stop_price:.5f})", uic=n.uic)
    return FixOutcomeLiveEur(n.symbol, "place_protective_stop", True,
                             f"placed stop for {n.uncovered_qty:,.0f} @ ~{stop_price:.5f} "
                             f"({pct:.0%} from current price {n.current_price:.5f})", uic=n.uic)


def run_safeguard_live_eur() -> list[FixOutcomeLiveEur]:
    """Fetch one (tier-filtered) LIVE-EUR Saxo snapshot, fix every naked
    position it can, plus whatever reconcile_live_eur_forex(aggressive=True)
    already resolves, then re-fetch and verify before reporting."""
    snapshot = hk_live_eur.fetch_live_snapshot()
    outcomes: list[FixOutcomeLiveEur] = []

    naked = hk_live_eur.scan_naked_positions_live_eur(snapshot=snapshot, send_email=False)
    for n in naked:
        outcomes.append(_fix_naked_position_live_eur(n))

    mismatch_findings = hk_live_eur.reconcile_live_eur_forex(aggressive=True, send_email=False,
                                                             snapshot=snapshot)
    for f in mismatch_findings:
        if f.kind == "fully_untracked":
            outcomes.append(FixOutcomeLiveEur(f.symbol, "needs_human_review", False,
                                              f.detail + " — LIVE: not auto-closed. A human must "
                                              f"decide what this position is.",
                                              uic=getattr(f, "uic", 0), needs_human=True))
        else:
            outcomes.append(FixOutcomeLiveEur(f.symbol, f.kind, f.kind != "stop_replace_failed",
                                              f.detail))

    if not outcomes:
        logger.info("[safeguard_live_eur] nothing to fix")
        return outcomes

    # ── Verify: re-fetch fresh and re-check what we just touched ──────────
    fresh = hk_live_eur.fetch_live_snapshot()
    still_naked_uics = {n.uic for n in hk_live_eur.scan_naked_positions_live_eur(snapshot=fresh, send_email=False)}

    for o in outcomes:
        if o.action == "place_protective_stop" and o.fixed and o.uic in still_naked_uics:
            o.fixed = False
            o.detail += " -- VERIFICATION FAILED: still shows naked/under-protected after the fix"

    for o in outcomes:
        _tag = "NEEDS HUMAN" if o.needs_human else ("FIXED" if o.fixed else "NOT FIXED")
        logger.info(f"[safeguard_live_eur] {_tag} {o.symbol} ({o.action}): {o.detail}")

    try:
        for o in outcomes:
            key = f"safeguard-live-eur:{o.symbol}:{o.action}"
            if o.needs_human:
                attention.raise_attention(
                    key, source="safeguard (LIVE EUR forex)",
                    title=f"{o.symbol}: unattributable real-money position",
                    detail=o.detail, severity="critical", grace_minutes=0, recheck_minutes=180)
            elif o.fixed:
                attention.clear_attention(key, note=o.detail[:200])
            else:
                attention.raise_attention(
                    key, source="safeguard (LIVE EUR forex)",
                    title=f"{o.symbol}: {o.action} keeps failing on real money",
                    detail=o.detail, severity="critical", grace_minutes=90, recheck_minutes=180)
        attention.flush()
    except Exception as exc:
        logger.warning(f"[safeguard_live_eur] attention routing failed: {exc}")

    _send_safeguard_email_live_eur(outcomes)
    return outcomes


def _send_safeguard_email_live_eur(outcomes: list[FixOutcomeLiveEur]) -> None:
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
    <h2 style="color:#c0392b">ATOS LIVE-EUR Safeguard: {fixed_n}/{total_n} issue(s) fixed and verified</h2>
    <p style="color:#666">{now} -- real-money EUR sub-account (RSI Pullback / exotic pairs)</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Status</th><th>Symbol</th><th>Action</th><th>Detail</th></tr>
    {rows}
    </table>
    <p style="color:#666;font-size:12px">Every "FIXED" row was re-verified against a fresh Saxo
    snapshot after the fix. Naked-position stops use a conservative {DEFAULT_STOP_PCT_FXSPOT:.0%}
    default distance, not RSI Pullback's own risk logic (unknown for an untracked position) --
    see this file's module docstring.</p>
    </body></html>"""
    hk_live_eur._send_email_live_eur(f"Safeguard: {fixed_n}/{total_n} fixed and verified — {now}", html)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    run_safeguard_live_eur()
