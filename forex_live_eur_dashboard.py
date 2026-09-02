"""
forex_live_eur_dashboard.py  —  Live (real-money) EUR sub-account dashboard
-----------------------------------------------------------------------------
Same idea as forex_live_dashboard.py (the SEK LIVE account's dashboard),
but for the EUR sub-account added 2026-08-26: RSI Pullback ONLY. As of
2026-08-28 this account trades the SAME 17-pair HIGH_VOLUME_SYMBOLS
universe as the SEK account (bb) -- no exotic pairs live any longer
("I want to test both strategies BB and RSI ... on 17 Pairs"). Sharing
the same 17 pairs across both accounts is safe because
housekeeping_live_eur.py's fetch_live_snapshot() now attributes each
pooled Saxo position/order to the correct account via its own
AccountKey field (verified live), not pair-tier membership -- see that
docstring for the full history. Legacy open positions from this
account's original EXOTIC_SYMBOLS design are still shown/tracked
alongside any new HIGH_VOLUME trades. Still sized off a 500 EUR
code-level cap (see forex_live_eur_account_2026-08-26 session notes for
why this is a code-level cap, not a broker-enforced wall -- Saxo pools
margin across all 3 of this Client's sub-accounts).

Deliberately THINNER than forex_live_dashboard.py: this account is
EUR-denominated, the SAME currency basis forex_dashboard.py's SIM
dashboard already uses -- no new SEK-style triangulation helper needed,
_eur_per_unit()/_fx_conversion_instruments() are reused directly from
forex_dashboard.py.

Usage:
    python forex_live_eur_dashboard.py            # refresh every 60s
    python forex_live_eur_dashboard.py --fast     # refresh every 10s
    python forex_live_eur_dashboard.py --once     # print once and exit
"""

import os, sys, json
from datetime import date, datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import price_service
import forex.runner as runner
from forex.universe import HIGH_VOLUME_SYMBOLS
import forex_dashboard as fd   # reuse its rendering helpers + EUR conversion

REFRESH_SECONDS = 60


def _read_positions() -> list:
    """Same shape as forex_dashboard._read_positions()/forex_live_dashboard's
    own reader, pointed at whichever state file forex.runner is currently
    on (must call runner.set_account_env('live_eur') before this)."""
    if not os.path.exists(runner.STATE_FILE):
        return []
    try:
        d = json.load(open(runner.STATE_FILE, encoding="utf-8"))
        positions = d.get("positions", {})
        out = []
        for key, pos in positions.items():
            strat, sym = key.split(":", 1) if ":" in key else ("rsi", key)
            out.append({
                "key": key, "strategy": strat, "symbol": sym,
                "uic": pos.get("uic"), "asset_type": pos.get("asset_type", "FxSpot"),
                "direction": pos.get("direction", "Buy"), "qty": pos.get("quantity", 0),
                "entry": float(pos.get("entry_price", 0)), "stop": float(pos.get("stop_price", 0)),
                "entry_date": pos.get("entry_date", ""), "atr": float(pos.get("atr_at_entry", 0)),
                "order_id": pos.get("order_id", "—"),
            })
        return out
    except Exception:
        return []


def _render(once: bool = False, interval: int = REFRESH_SECONDS) -> str:
    runner.set_account_env("live_eur")
    now_ts    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    positions = _read_positions()

    import saxo_auth
    try:
        token = saxo_auth.get_valid_access_token(env="live_eur")
    except Exception:
        token = None

    instruments = [{"symbol": p["symbol"], "uic": p["uic"], "asset_type": p["asset_type"]}
                   for p in positions if p.get("uic")]
    quote_ccys  = {p["symbol"][3:6] for p in positions if len(p.get("symbol", "")) >= 6}
    instruments += fd._fx_conversion_instruments(quote_ccys)

    live, price_src = price_service.fetch_prices(instruments, token=token, env="live")

    # NOTE: Saxo's /balances/me is pooled across all 3 sub-accounts under
    # this Client (confirmed 2026-08-26 -- an explicit AccountKey filter
    # makes no difference), so this always comes back as the GROUP total
    # in SEK, never a true isolated EUR figure for just this sub-account.
    # Showing it as if it were "this account's EUR equity" would be a real
    # money-figure error on a real-money dashboard -- labelled honestly as
    # the pooled group total instead. The 500 EUR CAP (shown separately,
    # from config) is what actually governs this account's sizing
    # regardless of what the pooled balance says.
    pooled_total = pooled_ccy = None
    try:
        import saxo_client
        bal = saxo_client.get_balances(env="live_eur")
        pooled_total = float(bal.get("TotalValue") or 0) or None
        pooled_ccy   = bal.get("Currency", "?")
    except Exception:
        pass
    import atos.capital_config as _cap
    cap_eur = _cap.forex_live_eur_risk_equity_eur()

    W_TOTAL = 139
    HR      = f"  {fd.DM}{'─' * W_TOTAL}{fd.W}"
    L       = []

    if price_src == "saxo":
        src_tag = "SAXO LIVE"
    elif not positions:
        src_tag = "n/a (no open positions to price)"
    else:
        src_tag = "n/a (token expired)"
    L.append(f"  {fd.BD}{fd.CY}╔{'═'*W_TOTAL}╗{fd.W}")
    L.append(f"  {fd.BD}{fd.CY}║{'  FOREX LIVE-EUR ACCOUNT — REAL MONEY':^{W_TOTAL}}║{fd.W}")
    L.append(f"  {fd.BD}{fd.CY}║{f'  RSI only  |  17 HIGH_VOLUME Pairs  |  Sizing Cap: {cap_eur:,.0f} EUR  |  Prices: {src_tag}  |  {now_ts}':^{W_TOTAL}}║{fd.W}")
    L.append(f"  {fd.BD}{fd.CY}╚{'═'*W_TOTAL}╝{fd.W}")
    L.append("")
    pooled_s = f"{pooled_total:,.0f} {pooled_ccy}" if pooled_total else "—"
    L.append(f"  {fd.DM}This is the REAL-MONEY EUR sub-account -- separate state/orders/P&L "
             f"from both SIM and the SEK LIVE account. Saxo pools margin/balance across "
             f"all 3 sub-accounts under this Client (confirmed: no true per-sub-account "
             f"balance figure exists) -- the {pooled_s} shown by Saxo's own balance API is "
             f"the whole Client Group's total, NOT this account's own equity. Sizing here "
             f"is governed entirely by the {cap_eur:,.0f} EUR code-level cap above "
             f"(atos.capital_config.forex_live_eur_risk_equity_eur()), not by that figure.{fd.W}")
    L.append(HR)
    L.append("")

    # ── Open positions ───────────────────────────────────────────────────
    lines, total_pnl, total_cost, _ = fd._positions_section(
        "OPEN POSITIONS", positions, live, {}, W_TOTAL, HR, color=fd.GR)
    L.extend(lines)
    if positions:
        tc = fd.GR if total_pnl >= 0 else fd.RD
        tpct = (total_pnl / total_cost * 100) if total_cost > 0 else 0
        L.append(
            f"  {fd.BD}TOTAL{fd.W}  "
            f"{fd.DM}{len(positions)} positions  |  Cost: €{total_cost:>,.0f}  |  "
            f"Unrealized P&L: {fd.W}{tc}{fd.BD}{total_pnl:>+,.2f} EUR  ({tpct:>+.4f}%){fd.W}"
        )
    L.append(HR)
    L.append("")

    # ── Strategy breakdown -- single table, module="forex_live_eur" ──────
    # Only rsi ever trades this account -- exclude the other 10 rather
    # than show 10 permanently-empty 0/0 rows.
    _not_live_eur = set(runner.STRATEGIES) - runner.LIVE_EUR_ALLOWED_STRATEGIES
    L.extend(fd._strategy_breakdown_table(
        "STRATEGY BREAKDOWN — LIVE-EUR (real money)",
        positions, live, symbols=HIGH_VOLUME_SYMBOLS, universe_size=len(HIGH_VOLUME_SYMBOLS),
        exclude=_not_live_eur, color=fd.GR, total_label="LIVE-EUR TOTAL",
        module="forex_live_eur", currency_label="EUR"))

    L.append(f"  {fd.DM}Realized/Today P&L above is via pnl_tracker module='forex_live_eur' "
             f"(fully separate ledger from SIM's 'forex' and the SEK account's 'forex_live').{fd.W}")
    if not once:
        L.append(f"  {fd.DM}Refreshes every {interval}s  |  Ctrl+C to exit{fd.W}")
    L.append("")
    return "\n".join(L)


def main():
    fast = "--fast" in sys.argv
    once = "--once" in sys.argv
    interval = 10 if fast else REFRESH_SECONDS

    if once:
        sys.stdout.write(_render(once=True, interval=interval))
        return

    import time
    while True:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(_render(once=False, interval=interval))
        sys.stdout.flush()
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
