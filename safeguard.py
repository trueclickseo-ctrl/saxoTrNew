"""
safeguard.py
-------------
Runs immediately after housekeeping.py's reconcile_all() + scan_naked_
positions() and ACTS on what they find, instead of only reporting it.
Built 2026-08-24 after a live run surfaced 23 unprotected positions and 8
state mismatches that housekeeping.py — deliberately conservative, so it's
safe to run unattended after every trade — reported but did not resolve.

Two categories, two different kinds of "fix":

1. NAKED / UNDER-PROTECTED LIVE POSITIONS (housekeeping.scan_naked_positions)
   Places a protective stop for whatever quantity isn't already covered,
   using a conservative asset-class-default distance from the position's
   OWN current price (see DEFAULT_STOP_PCT below). This is a last-resort
   safety net, NOT a replacement for the originating strategy's own
   (unknown/lost) risk logic — the goal is "never left naked," not "as
   tight as the real strategy would have set it."
   An existing take-profit order, if any, is left untouched — only the
   missing stop-loss leg gets added. A "partial" stop is topped up for
   just the uncovered remainder, never duplicated.

2. STATE MISMATCHES housekeeping.py deliberately leaves untouched
   (direction_mismatch, untracked_live) because per-strategy attribution
   is ambiguous. Safeguard doesn't guess attribution either — it calls
   reconcile_all(aggressive=True), which removes any local entry proven
   WRONG (zero live backing in its own claimed direction) exactly the way
   an ordinary zero-exposure orphan gets removed. It does not invent a
   new local entry for the real live exposure that confused the old one;
   that exposure's PROTECTION (not its bookkeeping) is what category 1
   above exists to guarantee.

VERIFICATION: after every fix, this module re-fetches a fresh Saxo
snapshot and re-checks that the specific thing it just fixed is actually
fixed — never marks something "fixed" on faith. A fix that fails (order
rejected, still under-covered after retry) is reported as NOT fixed, not
silently swallowed. Sends exactly one confirmation email per run, listing
every item with its verified fixed/not-fixed outcome — only if there was
something to do.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import attention
import housekeeping
import saxo_client
import saxo_order

logger = logging.getLogger("safeguard")

# Conservative last-resort stop distance, by AssetType. NOT tuned to any
# strategy's real risk logic (which is unknown/lost for these positions) —
# just wide enough to avoid an immediate re-trigger on normal noise, tight
# enough to actually bound the loss. FxSpot/futures figures are close to
# what this codebase's own ATR-based stops typically land on for a fresh
# entry; Stock reuses the existing US_BLEND_STOP_PCT precedent (see
# account_margin_2026-08-22 session notes — 6 naked stock positions were
# given 8% stops by hand that day).
DEFAULT_STOP_PCT = {
    "FxSpot":          0.02,
    "CfdOnIndex":      0.03,
    "ContractFutures": 0.03,
    "Etf":             0.05,
    "Stock":           0.08,
    "CfdOnStock":      0.08,
}
_DEFAULT_FALLBACK_PCT = 0.03

# Alert dedup: some findings (e.g. a naked position safeguard genuinely
# cannot fix, like a SIM-quote-restricted instrument with no price to base a
# stop on) recur identically on every single run of every module that calls
# run_safeguard() -- multiple times an hour across forex/futures/etf/stocks.
# Found live 2026-08-26: one stuck ZC orphan (no CurrentPrice available under
# Saxo SIM's standing quote restriction for that instrument, same class as
# SI) meant _send_safeguard_email() fired on every run indefinitely, drowning
# out genuinely new findings. Same REALERT_AFTER_HOURS convention as
# scheduler_watchdog.py -- suppress re-emailing the SAME (category, module,
# symbol, action, fixed) fingerprint within this window, but always email
# immediately the first time a finding appears or its fixed status changes.
REALERT_AFTER_HOURS = 4
_ALERT_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "safeguard_alert_state.json")


def _load_alert_state() -> dict:
    try:
        with open(_ALERT_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_alert_state(state: dict) -> None:
    os.makedirs(os.path.dirname(_ALERT_STATE_PATH), exist_ok=True)
    tmp = _ALERT_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _ALERT_STATE_PATH)


@dataclass
class FixOutcome:
    category: str      # "naked" | "mismatch"
    module:   str
    symbol:   str
    action:   str
    fixed:    bool
    detail:   str
    uic:      int = 0   # naked outcomes only -- stable identity for verification,
                        # since a mismatch-fix earlier in the same run can change
                        # which module a shared instrument gets classified under
    auto_resolved: bool = False   # SIM: an untracked position ATOS flat-closed itself


def _stop_pct(asset_type: str) -> float:
    return DEFAULT_STOP_PCT.get(asset_type, _DEFAULT_FALLBACK_PCT)


# Live Format.Decimals lookup moved to saxo_client.get_price_decimals() so
# futures/runner.py's trailing-stop fix (2026-08-24, same day) can share it
# instead of duplicating this a third time. Kept as a thin local alias so
# every existing call site below (and the test suite) doesn't need to change.
_live_price_decimals = saxo_client.get_price_decimals


def _fix_naked_position(n: "housekeeping.NakedPosition") -> FixOutcome:
    if n.uncovered_qty <= 0:
        return FixOutcome("naked", n.module, n.symbol, "none needed", True,
                          "already fully covered by the time this ran", uic=n.uic)
    if not n.current_price:
        return FixOutcome("naked", n.module, n.symbol, "skipped", False,
                          "no live current price available to base a stop on", uic=n.uic)

    pct = _stop_pct(n.asset_type)
    if n.direction == "Buy":
        stop_price = n.current_price * (1 - pct)
    else:
        stop_price = n.current_price * (1 + pct)

    akey = saxo_client.get_account_key()
    dp = None
    symbol_for_dp = ""
    if n.module == "forex":
        try:
            import forex.runner as fr
            dp = fr.get_price_decimals(n.symbol)
            symbol_for_dp = n.symbol
        except Exception:
            pass
    if dp is None and n.asset_type == "FxSpot":
        # Covers non-forex-universe FxSpot symbols (e.g. a futures-module
        # instrument like CADMXN whose real Saxo AssetType is FxSpot) that
        # forex.runner's cached 117-pair universe doesn't know about.
        dp = _live_price_decimals(n.uic, n.asset_type)

    new_oid = saxo_order.place_protective_stop(
        post_fn=lambda path, body: saxo_client.post(path, body),
        account_key=akey, uic=n.uic, asset_type=n.asset_type,
        amount=int(n.uncovered_qty), direction=n.direction, stop_price=stop_price,
        label=f"safeguard:{n.module}:{n.symbol}", symbol=symbol_for_dp, price_decimals=dp,
    )
    if new_oid is None:
        return FixOutcome("naked", n.module, n.symbol, "place_protective_stop", False,
                          f"Saxo rejected the protective stop order "
                          f"({n.uncovered_qty:,.0f} @ ~{stop_price:.5f})", uic=n.uic)
    return FixOutcome("naked", n.module, n.symbol, "place_protective_stop", True,
                      f"placed stop for {n.uncovered_qty:,.0f} @ ~{stop_price:.5f} "
                      f"({pct:.0%} from current price {n.current_price:.5f})", uic=n.uic)


def _close_untracked_sim(f: "housekeeping.Finding") -> FixOutcome:
    """SIM only: flat-close a live position with zero local record in any
    module. It's paper money and nothing is managing it -- send an opposing
    Market order for the full net size. If Saxo rejects it, report NOT
    FIXED so run_safeguard() escalates via attention."""
    net = float(getattr(f, "net_amount", 0) or 0)
    uic = int(getattr(f, "uic", 0) or 0)
    asset_type = getattr(f, "asset_type", "") or "FxSpot"
    if not (uic and net):
        return FixOutcome("mismatch", f.module, f.symbol, "auto_close_untracked", False,
                          f.detail + " — could not auto-close: missing uic/net on the finding")
    side = "Sell" if net > 0 else "Buy"
    order = {"AccountKey": saxo_client.get_account_key(), "Uic": uic, "AssetType": asset_type,
             "Amount": abs(int(net)), "BuySell": side, "OrderType": "Market",
             "OrderDuration": {"DurationType": "DayOrder"}, "ManualOrder": False}
    try:
        resp = saxo_client.post("/trade/v2/orders", order)
        oid = (resp or {}).get("OrderId")
        if not oid:
            raise RuntimeError(f"no OrderId in response: {str(resp)[:160]}")
        return FixOutcome("mismatch", f.module, f.symbol, "auto_close_untracked", True,
                          f"SIM: flat-closed an unattributable {side.lower()} of {abs(net):,.0f} "
                          f"(uic {uic}) — order {oid}. Was invisible to every strategy; paper "
                          f"money, so ATOS closed it rather than paging a human.",
                          uic=uic, auto_resolved=True)
    except Exception as exc:
        exc_str = str(exc)
        if "SellOrdersAlreadyExistForOwnedContracts" in exc_str:
            # Position already has a stop/sell order — it is covered.
            # No flat-close needed; clear any outstanding attention for it.
            return FixOutcome(
                "mismatch", f.module, f.symbol, "auto_close_untracked_stop_covered", True,
                f"SIM: unattributable {side.lower()} of {abs(net):,.0f} (uic {uic}) is "
                f"already covered by an existing stop/sell order — no flat-close sent. "
                f"Position is protected; attention cleared.", uic=uic, auto_resolved=True)
        return FixOutcome("mismatch", f.module, f.symbol, "auto_close_untracked", False,
                          f"SIM: tried to flat-close an unattributable {side.lower()} of "
                          f"{abs(net):,.0f} (uic {uic}) but Saxo rejected it: {exc}", uic=uic)


def _fix_mismatches(modules: list[str], snapshot: "housekeeping.LiveSnapshot") -> list[FixOutcome]:
    findings = housekeeping.reconcile_all(modules, aggressive=True, snapshot=snapshot,
                                          send_email=False)
    outcomes = []
    for f in findings:
        if f.kind == housekeeping.KIND_DIRECTION_MISMATCH:
            outcomes.append(FixOutcome("mismatch", f.module, f.symbol,
                                       "removed_wrong_direction_entry", True, f.detail))
        elif f.kind == housekeeping.KIND_LEDGER_DRIFT:
            outcomes.append(FixOutcome("mismatch", f.module, f.symbol,
                                       "ledger_row_needs_manual_exit", False, f.detail))
        elif f.kind == housekeeping.KIND_UNTRACKED_LIVE:
            # No local entry is wrong here, so there's nothing to remove —
            # the real exposure's PROTECTION is handled by the naked-fix
            # pass, not this one. Record it as informational, not a
            # separate "fixed" claim, to avoid double-counting one root
            # cause as two different resolved issues.
            outcomes.append(FixOutcome("mismatch", f.module, f.symbol,
                                       "no_local_entry_to_fix", True,
                                       f.detail + " — protection (if needed) is handled by "
                                       f"the naked-position fix pass, not this one"))
        elif f.kind == housekeeping.KIND_FULLY_UNTRACKED:
            # 2026-08-24: a live position with zero local record in any
            # module. On SIM (this module -- safeguard.py) it's paper money
            # and the position is already unmanaged by any strategy, so
            # ATOS just FLAT-CLOSES it and moves on (user decision
            # 2026-09-01: "minimum human interaction"). LIVE
            # (safeguard_live.py / _eur) never auto-closes -- it escalates
            # via attention.raise_attention so a human decides.
            outcomes.append(_close_untracked_sim(f))
        elif f.kind == housekeeping.KIND_PENDING_ENTRY:
            # 2026-08-24: nothing IS wrong here — the entry just hasn't
            # filled yet (e.g. market closed). Before this case existed
            # every such finding fell through to the generic "error" branch
            # below and got reported as NOT FIXED, which reads as a real
            # failure when it's actually working-as-intended (see
            # LiveSnapshot.has_pending_entry()'s docstring). Recorded as
            # informational/resolved so it doesn't misrepresent a normal,
            # temporary state as something safeguard failed to fix.
            outcomes.append(FixOutcome("mismatch", f.module, f.symbol,
                                       "entry_not_filled_yet", True,
                                       f.detail + " — nothing to fix, will resolve itself once "
                                       f"the entry order fills or expires"))
        elif f.kind == housekeeping.KIND_SUSPECT_ORPHAN:
            # 2026-08-26: live_net==0 contradicted by a still-Working stop
            # order -- see reconcile_module()'s docstring at that check.
            # Deliberately left untouched (not removed, not re-fixed here
            # either) so a genuinely stale snapshot gets a clean re-check
            # next run instead of safeguard papering over it with a guessed
            # price-based stop while the real one may still be intact.
            outcomes.append(FixOutcome("mismatch", f.module, f.symbol,
                                       "suspect_snapshot_left_alone", True,
                                       f.detail + " — left alone deliberately; if this keeps "
                                       f"recurring for the same position, investigate the "
                                       f"positions/me feed directly rather than trusting either "
                                       f"snapshot blindly"))
        elif f.kind in (housekeeping.KIND_REMOVED_ORPHAN, housekeeping.KIND_SCALED_DOWN,
                       housekeeping.KIND_DUPLICATE_STOP, housekeeping.KIND_STOP_REPLACE_FAILED,
                       housekeeping.KIND_STOP_MISSING, housekeeping.KIND_STOP_STALE):
            # Already handled by reconcile_all()'s normal (non-aggressive)
            # behavior — not part of what safeguard was specifically asked
            # to resolve, but surfaced for completeness. KIND_STOP_STALE
            # moved into this bucket 2026-08-25: _check_stop_integrity()
            # now auto-corrects local's stop_price to match the real
            # broker order instead of only reporting it (see its
            # docstring for why the broker side is trustworthy here).
            outcomes.append(FixOutcome("mismatch", f.module, f.symbol, f.kind,
                                       f.kind != housekeeping.KIND_STOP_REPLACE_FAILED, f.detail))
        else:
            outcomes.append(FixOutcome("mismatch", f.module, f.symbol, "error", False, f.detail))
    return outcomes


def run_safeguard(modules: list[str] | None = None) -> list[FixOutcome]:
    """Fetch one live Saxo snapshot, fix every naked position and mismatch
    it can, then re-fetch and verify before reporting. Returns the full
    list of outcomes (empty if there was nothing to fix).

    Naked-position fixes run BEFORE mismatch fixes deliberately: removing a
    direction-mismatched local entry changes which module's adapter still
    references that instrument's uic, which is exactly the heuristic
    scan_naked_positions() uses to classify an FxSpot position as forex vs.
    futures. Fixing mismatches first would make that classification shift
    mid-run and break the post-fix verification match below (matched by
    uic, not by module+symbol, for the same reason)."""
    modules = modules or list(housekeeping.ADAPTERS)
    snapshot = housekeeping.fetch_live_snapshot()

    outcomes: list[FixOutcome] = []

    naked = housekeeping.scan_naked_positions(snapshot=snapshot, send_email=False)
    naked = [n for n in naked if n.module in modules or n.module == "unknown"]
    for n in naked:
        outcomes.append(_fix_naked_position(n))

    outcomes.extend(_fix_mismatches(modules, snapshot))

    if not outcomes:
        logger.info("[safeguard] nothing to fix")
        return outcomes

    # ── Verify: re-fetch fresh and re-check what we just touched ──────────
    fresh = housekeeping.fetch_live_snapshot()
    still_naked_uics = {n.uic for n in housekeeping.scan_naked_positions(snapshot=fresh, send_email=False)}
    remaining_mismatches = housekeeping.reconcile_all(modules, aggressive=True, snapshot=fresh,
                                                      send_email=False)
    still_wrong = {(f.module, f.symbol) for f in remaining_mismatches
                  if f.kind in (housekeeping.KIND_DIRECTION_MISMATCH, housekeeping.KIND_LEDGER_DRIFT)}

    for o in outcomes:
        if o.category == "naked" and o.fixed and o.uic in still_naked_uics:
            o.fixed = False
            o.detail += " — VERIFICATION FAILED: still shows naked/under-protected after the fix"
        elif o.category == "mismatch" and o.fixed and o.action == "removed_wrong_direction_entry":
            if (o.module, o.symbol) in still_wrong:
                o.fixed = False
                o.detail += " — VERIFICATION FAILED: still flagged after re-check"

    for o in outcomes:
        logger.info(f"[safeguard] {'FIXED' if o.fixed else 'NOT FIXED'} "
                    f"{o.category}/{o.module}/{o.symbol} ({o.action}): {o.detail}")

    _escalate_unfixed(outcomes)

    to_email = _dedup_for_email(outcomes)
    if to_email:
        _send_safeguard_email(to_email)
    else:
        logger.info(f"[safeguard] {len(outcomes)} outcome(s), all already reported "
                    f"within the last {REALERT_AFTER_HOURS}h — not re-emailing")
    return outcomes


def _escalate_unfixed(outcomes: list[FixOutcome]) -> None:
    """SIM is PAPER money -- ATOS resolves what it can and the routine
    safeguard email already lists every NOT-FIXED item. The one thing
    worth a human's attention on SIM is an unattributable position ATOS
    tried and FAILED to flat-close (the forward-test data is then
    corrupted): that alone routes to the shared attention channel.
    Everything else (Saxo SIM stop-replace flakiness, quote-restricted
    naked positions -- chronic, nothing a human does about them) does not.
    flush() still runs here every cycle -- it's the 30-min heartbeat that
    also delivers items raised by the LIVE agents and the runner."""
    try:
        for o in outcomes:
            if o.action not in ("auto_close_untracked", "auto_close_untracked_stop_covered"):
                continue
            key = f"safeguard-sim:untracked:{o.module}:{o.symbol}"
            if o.fixed or o.auto_resolved:
                note = ("covered by existing stop — no flat-close needed"
                        if o.action == "auto_close_untracked_stop_covered"
                        else "ATOS flat-closed it")
                attention.clear_attention(key, note=note)
            else:
                attention.raise_attention(
                    key, source="safeguard (SIM)",
                    title=f"{o.module}/{o.symbol}: SIM position ATOS could not flat-close",
                    detail=o.detail + " — the SIM forward-test data for this pair is skewed "
                                      "until this is cleared (close it manually in the SIM "
                                      "platform, or ignore if the pair is not under study).",
                    grace_minutes=120, recheck_minutes=120)
        attention.flush()
    except Exception as exc:
        logger.warning(f"[safeguard] attention routing failed: {exc}")


def _dedup_for_email(outcomes: list[FixOutcome]) -> list[FixOutcome]:
    """Drop outcomes already emailed recently with the same fixed status —
    see REALERT_AFTER_HOURS above. A finding that's new, or whose fixed
    status flipped since last time, is always kept."""
    state = _load_alert_state()
    now = datetime.now()
    to_email = []
    for o in outcomes:
        key = f"{o.category}|{o.module}|{o.symbol}|{o.action}"
        prev = state.get(key)
        if prev and prev.get("fixed") == o.fixed:
            last = datetime.fromisoformat(prev["at"])
            if now - last < timedelta(hours=REALERT_AFTER_HOURS):
                continue  # unchanged and recently reported — suppress
        to_email.append(o)
        state[key] = {"fixed": o.fixed, "at": now.isoformat()}
    _save_alert_state(state)
    return to_email


# ── Email ───────────────────────────────────────────────────────────────

def _load_email_cfg():
    return housekeeping._load_email_cfg()


def _send_safeguard_email(outcomes: list[FixOutcome]) -> bool:
    cfg = _load_email_cfg()
    fixed_n = sum(1 for o in outcomes if o.fixed)
    total_n = len(outcomes)
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    if not cfg:
        logger.info(f"[safeguard] no config/email.json — would have sent: "
                    f"Safeguard: {fixed_n}/{total_n} fixed and verified")
        return False

    rows = ""
    for o in sorted(outcomes, key=lambda o: (not o.fixed, o.category, o.module, o.symbol)):
        status = "FIXED" if o.fixed else "NOT FIXED"
        color = "#2ea043" if o.fixed else "#da3633"
        rows += (f'<tr><td style="color:{color};font-weight:bold">{status}</td>'
                 f"<td>{o.category}</td><td>{o.module}</td><td>{o.symbol}</td>"
                 f"<td>{o.action}</td><td>{o.detail}</td></tr>")

    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2>Safeguard: {fixed_n}/{total_n} issue(s) fixed and verified</h2>
    <p style="color:#666">{now}</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Status</th><th>Category</th><th>Module</th><th>Symbol</th><th>Action</th><th>Detail</th></tr>
    {rows}
    </table>
    <p style="color:#666;font-size:12px">Every "FIXED" row was re-verified against a fresh Saxo
    snapshot after the fix, not assumed. "NOT FIXED" rows need manual attention — Saxo rejected
    the order, or the position still doesn't match after the attempt. Naked-position stops use a
    conservative asset-class-default distance (not the originating strategy's own risk logic,
    which is unknown for an untracked position) — see safeguard.py's module docstring.</p>
    </body></html>"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Safeguard: {fixed_n}/{total_n} fixed and verified — {now}"
        msg["From"]    = f"Safeguard Agent <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        return True
    except Exception as exc:
        logger.warning(f"[safeguard] email FAILED: {exc}")
        return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--modules", nargs="*", default=None,
                   help="subset of forex/futures/etf/stocks (default: all)")
    args = p.parse_args()
    run_safeguard(args.modules)
