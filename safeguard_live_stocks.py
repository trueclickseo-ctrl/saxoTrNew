"""
safeguard_live_stocks.py
------------------------
Auto-fix agent for the real-money US Blend stocks sleeve (atos_live_stocks.py)
ONLY. Runs after housekeeping_live_stocks.py's scans and ACTS on what they
find -- same relationship SIM's safeguard.py has to housekeeping.py, but a
fully separate file. Separate from safeguard_live.py (LIVE forex) too.

ONE category of automated action: a NAKED / under-protected real-money stock
position gets a conservative protective stop (8%, matching US Blend's own
US_BLEND_STOP_PCT) for the uncovered quantity, from its OWN current price.
Every fix is re-verified against a fresh snapshot.

LIVE NEVER AUTO-CLOSES. An untracked / unattributable stock position is
escalated to the one 'ATOS needs a human' channel (attention.py,
"live_stocks:...") -- immediately (grace 0), because it is real money on an
account also running the forex wind-down.

Phase 1: with zero open positions this runs as a clean no-op every cycle;
it exists so the safety net predates the first real fill.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import attention
import saxo_client
import saxo_order

import housekeeping_live_stocks as hk
from housekeeping_live import _send_email_live

logger = logging.getLogger("safeguard_live_stocks")

# Match US Blend's own stop convention (atos_runner.US_BLEND_STOP_PCT) rather
# than forex's 2% -- a stock's normal daily range is wider.
DEFAULT_STOP_PCT_STOCK = 0.08
_STOCK_PRICE_DECIMALS = 2


@dataclass
class FixOutcome:
    symbol: str
    action: str
    fixed: bool
    detail: str
    uic: int = 0
    needs_human: bool = False


def _fix_naked_stock(n: "hk.NakedStockLive") -> FixOutcome:
    if n.uncovered_qty <= 0:
        return FixOutcome(n.symbol, "none needed", True, "already covered by the time this ran", uic=n.uic)
    if not n.current_price:
        return FixOutcome(n.symbol, "skipped", False, "no live current price to base a stop on", uic=n.uic)

    pct = DEFAULT_STOP_PCT_STOCK
    stop_price = n.current_price * (1 - pct) if n.direction == "Buy" else n.current_price * (1 + pct)
    akey = saxo_client.get_account_key(env="live")

    new_oid = saxo_order.place_protective_stop(
        post_fn=lambda path, body: saxo_client.post(path, body, env="live"),
        account_key=akey, uic=n.uic, asset_type="Stock",
        amount=int(n.uncovered_qty), direction=n.direction, stop_price=stop_price,
        label=f"safeguard_live_stocks:{n.symbol}", symbol=n.symbol,
        price_decimals=_STOCK_PRICE_DECIMALS,
    )
    if new_oid is None:
        return FixOutcome(n.symbol, "place_protective_stop", False,
                          f"Saxo rejected the protective stop ({n.uncovered_qty:,.0f} @ ~{stop_price:.2f})",
                          uic=n.uic)
    if not hk.stop_order_is_working(new_oid):
        saxo_client.cancel_order(new_oid, env="live")
        return FixOutcome(n.symbol, "place_protective_stop", False,
                          f"Saxo returned id {new_oid} but it never became a live order "
                          f"-- still unprotected, retry next cycle", uic=n.uic)
    return FixOutcome(n.symbol, "place_protective_stop", True,
                      f"placed stop for {n.uncovered_qty:,.0f} sh @ ~{stop_price:.2f} "
                      f"({pct:.0%} from {n.current_price:.2f})", uic=n.uic)


def run_safeguard_live_stocks() -> list[FixOutcome]:
    snapshot = hk.fetch_live_stock_snapshot()
    outcomes: list[FixOutcome] = []

    first_naked = hk.scan_naked_stock_positions(snapshot=snapshot, send_email=False)
    for n in hk.confirm_naked_stock_live(snapshot, first_naked):
        outcomes.append(_fix_naked_stock(n))

    for f in hk.reconcile_live_stocks(send_email=False, snapshot=snapshot):
        if f.kind in ("fully_untracked", "untracked_local"):
            outcomes.append(FixOutcome(f.symbol, "needs_human_review", False,
                                       f.detail + " — LIVE: not auto-closed.",
                                       uic=getattr(f, "uic", 0), needs_human=True))
        else:
            outcomes.append(FixOutcome(f.symbol, f.kind, False, f.detail, needs_human=True))

    if not outcomes:
        logger.info("[safeguard_live_stocks] nothing to fix")
        return outcomes

    fresh = hk.fetch_live_stock_snapshot()
    if not hk.orders_snapshot_looks_unreliable(fresh):
        still_naked = {n.uic for n in hk.scan_naked_stock_positions(snapshot=fresh, send_email=False)}
        for o in outcomes:
            if o.action == "place_protective_stop" and o.fixed and o.uic in still_naked:
                o.fixed = False
                o.detail += " -- VERIFICATION FAILED: still naked after the fix"

    for o in outcomes:
        tag = "NEEDS HUMAN" if o.needs_human else ("FIXED" if o.fixed else "NOT FIXED")
        logger.info(f"[safeguard_live_stocks] {tag} {o.symbol} ({o.action}): {o.detail}")

    _escalate(outcomes)
    _send_email(outcomes)
    return outcomes


def _escalate(outcomes: list[FixOutcome]) -> None:
    try:
        for o in outcomes:
            key = f"safeguard-live-stocks:{o.symbol}:{o.action}"
            if o.needs_human:
                attention.raise_attention(
                    key, source="safeguard (LIVE stocks)",
                    title=f"{o.symbol}: unattributable real-money stock position",
                    detail=o.detail, severity="critical",
                    grace_minutes=0, recheck_minutes=180)
            elif o.fixed:
                attention.clear_attention(key, note=o.detail[:200])
            else:
                attention.raise_attention(
                    key, source="safeguard (LIVE stocks)",
                    title=f"{o.symbol}: {o.action} keeps failing on real money",
                    detail=o.detail, severity="critical",
                    grace_minutes=90, recheck_minutes=180)
        attention.flush()
    except Exception as exc:
        logger.warning(f"[safeguard_live_stocks] attention routing failed: {exc}")


def _send_email(outcomes: list[FixOutcome]) -> None:
    fixed_n = sum(1 for o in outcomes if o.fixed)
    now = datetime.now().strftime("%d %b %Y  %H:%M PKT")
    rows = ""
    for o in sorted(outcomes, key=lambda o: (not o.fixed, o.symbol)):
        status = "FIXED" if o.fixed else ("NEEDS HUMAN" if o.needs_human else "NOT FIXED")
        color = "#2ea043" if o.fixed else "#da3633"
        rows += (f'<tr><td style="color:{color};font-weight:bold">{status}</td>'
                 f"<td>{o.symbol}</td><td>{o.action}</td><td>{o.detail}</td></tr>")
    html = f"""<!DOCTYPE html><html><body style="font-family:sans-serif">
    <h2 style="color:#c0392b">ATOS LIVE STOCKS Safeguard: {fixed_n}/{len(outcomes)} fixed & verified</h2>
    <p style="color:#666">{now} — real-money US Blend sleeve</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
    <tr><th>Status</th><th>Symbol</th><th>Action</th><th>Detail</th></tr>{rows}</table>
    <p style="color:#666;font-size:12px">Naked-stock stops use a conservative
    {DEFAULT_STOP_PCT_STOCK:.0%} default. LIVE never auto-closes an untracked position.</p>
    </body></html>"""
    _send_email_live(f"STOCKS Safeguard: {fixed_n}/{len(outcomes)} fixed — {now}", html)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    run_safeguard_live_stocks()
