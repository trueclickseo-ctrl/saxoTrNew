"""
forex_live_dashboard.py  —  Live (real-money) Forex positions dashboard
------------------------------------------------------------------------
Same idea as forex_dashboard.py, but for the real-money Saxo LIVE account
(2026-08-25) instead of SIM: 1 strategy (bb only, as of 2026-08-28's
two-account pilot -- history: donchian/ema/rsi -> bb/rsi -> briefly
bb/rsi/pullback -> bb/rsi -> bb-only once rsi moved to the LIVE EUR
account), the full 17-pair HIGH_VOLUME_SYMBOLS universe (narrowed
2026-08-27 from all 34 CORE_SYMBOLS). A same-day (2026-08-28) attempt to
narrow this further to a 9-pair HIGH_VOLUME_GROUP_A subset was explicitly
reverted by the user before being committed -- the EUR account currently
has zero live pairs of its own (new entries paused, see forex/runner.py's
LIVE_EUR_ALLOWED_STRATEGIES comment), so this account safely keeps the
whole 17-pair set. SEK-denominated equity (6,000 SEK opening balance)
instead of SIM's EUR demo credit. No core/exotic split needed here -- the
live account is 100% core by construction.

Deliberately thin: reuses forex_dashboard.py's _positions_section() /
_strategy_breakdown_table() / _section_header() rendering helpers
directly rather than duplicating them, and forex.runner.set_account_env()
for state-file/gateway redirection -- the only genuinely new logic here is
the SEK currency-conversion path (SIM's dashboard converts to EUR; this
account's base currency is SEK, a different set of triangulation pairs).

Usage:
    python forex_live_dashboard.py            # refresh every 60s
    python forex_live_dashboard.py --fast      # refresh every 10s
    python forex_live_dashboard.py --once      # print once and exit
"""

import os, sys, time
from datetime import date, datetime

# Matches futures_dashboard.py's / forex_dashboard.py's own safeguard --
# without this, any invocation whose stdout isn't already UTF-8 (piped/
# redirected output, a non-UTF-8 console codepage) crashes with
# UnicodeEncodeError on the box-drawing characters used throughout.
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import price_service
import pnl_tracker
import forex.runner as runner
from forex.universe import PAIRS as _UNIVERSE_PAIRS, HIGH_VOLUME_SYMBOLS
import forex_dashboard as fd   # reuse its rendering helpers + colors

# 5 of the 34 CORE pairs are NOK/SEK/DKK crosses (paired against EUR/USD only
# -- distinct from the 32-pair dedicated SCANDI_SYMBOLS tier, which is SIM-
# only and never reaches this LIVE account). Added 2026-08-26 at explicit
# user request: watch these specifically as real LIVE trades accumulate --
# decide after the first ~10 closed trades whether to keep them in CORE or
# pull them, the same review process already applied to SCANDI/EXOTIC on SIM.
SCANDI_CORE_PAIRS = {"EURNOK", "EURSEK", "USDNOK", "USDSEK", "USDDKK"}
SCANDI_REVIEW_TRADE_COUNT = 10

_UNIVERSE_BY_SYMBOL = {p["symbol"]: p for p in _UNIVERSE_PAIRS}
REFRESH_SECONDS = 60

# ── Quote-currency -> SEK conversion ───────────────────────────────────────
# The LIVE account is SEK-denominated (unlike SIM's EUR demo credit), so
# forex_dashboard.py's EUR-based _eur_per_unit() doesn't apply here -- every
# CORE pair's quote currency needs its own SEK rate. Only USDSEK is a direct
# SEK pair among the 34 core pairs (EURSEK exists too, but EUR is never a
# quote currency in this pair set, so it's never needed for this). Every
# other quote currency (JPY/CAD/CHF/GBP/AUD/NZD/NOK/DKK/MXN) triangulates
# through its own USD leg + USDSEK -- one extra hop, same live-Saxo-only
# rule as SIM's dashboard (see forex_dashboard.py's own docstring on why
# Yahoo is never used here).
_SEK_RATE_CACHE: dict = {}


def _fx_conversion_instruments_sek(quote_ccys) -> list[dict]:
    """Saxo instruments needed to convert each of `quote_ccys` to SEK:
    USDSEK is always needed (the anchor), plus whichever of USD{ccy} /
    {ccy}USD exists in the universe for each currency actually held."""
    needed, seen = [], set()
    p = _UNIVERSE_BY_SYMBOL.get("USDSEK")
    if p:
        needed.append({"symbol": "USDSEK", "uic": p["uic"], "asset_type": "FxSpot"})
        seen.add("USDSEK")
    for ccy in quote_ccys:
        if ccy in ("SEK", "") or ccy in seen:
            continue
        p = _UNIVERSE_BY_SYMBOL.get(f"USD{ccy}")
        if p:
            seen.add(f"USD{ccy}")
            needed.append({"symbol": f"USD{ccy}", "uic": p["uic"], "asset_type": "FxSpot"})
            continue
        p = _UNIVERSE_BY_SYMBOL.get(f"{ccy}USD")
        if p:
            seen.add(f"{ccy}USD")
            needed.append({"symbol": f"{ccy}USD", "uic": p["uic"], "asset_type": "FxSpot"})
    return needed


def _sek_per_unit(ccy: str, live_prices: dict | None = None) -> float | None:
    """SEK value of one unit of `ccy`, from Saxo's live quotes only.
    Returns None if Saxo has no quote for the needed pair(s) this cycle --
    callers must treat that as "unknown," same convention as the SIM
    dashboard's _eur_per_unit()."""
    if ccy == "SEK":
        return 1.0
    if ccy in _SEK_RATE_CACHE:
        return _SEK_RATE_CACHE[ccy]

    live_prices = live_prices or {}
    usdsek = live_prices.get("USDSEK")
    rate = None
    if usdsek:
        if ccy == "USD":
            rate = usdsek
        else:
            usd_leg = live_prices.get(f"USD{ccy}")      # USD is base: 1 ccy = (1/price) USD
            if usd_leg:
                rate = usdsek / usd_leg
            else:
                inv_leg = live_prices.get(f"{ccy}USD")   # ccy is base: 1 ccy = price USD
                if inv_leg:
                    rate = usdsek * inv_leg

    if rate is not None:
        _SEK_RATE_CACHE[ccy] = rate
    return rate


def _read_positions() -> list:
    """Same shape as forex_dashboard._read_positions(), reading whichever
    state file forex.runner is currently pointed at (must call
    runner.set_account_env('live') before this)."""
    if not os.path.exists(runner.STATE_FILE):
        return []
    try:
        import json
        d = json.load(open(runner.STATE_FILE, encoding="utf-8"))
        positions = d.get("positions", {})
        out = []
        for key, pos in positions.items():
            strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
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
    runner.set_account_env("live")
    now_ts    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    positions = _read_positions()
    # Real refresh-capable token fetch, not price_service.load_token()'s
    # passive file-peek (which returns None on an expired access token and
    # never tries to refresh it). Found 2026-08-25: with LIVE's 20-min
    # access-token / 1-hour refresh-token lifetime and the keepalive task's
    # 15-min polling interval, there's a real recurring few-minute window
    # where the access token has expired but the keepalive hasn't ticked
    # again yet -- during that window the dashboard's own price fetch
    # would see load_token() return None and render blank "Now" prices,
    # even though the underlying refresh_token was perfectly healthy and
    # a real refresh would have succeeded immediately. get_valid_access_
    # token() actually performs that refresh instead of just checking a
    # cached expiry timestamp.
    import saxo_auth
    try:
        token = saxo_auth.get_valid_access_token(env="live")
    except Exception:
        token = None

    instruments = [{"symbol": p["symbol"], "uic": p["uic"], "asset_type": p["asset_type"]}
                   for p in positions if p.get("uic")]
    quote_ccys  = {p["symbol"][3:6] for p in positions if len(p.get("symbol", "")) >= 6}
    instruments += _fx_conversion_instruments_sek(quote_ccys)

    live, price_src = price_service.fetch_prices(instruments, token=token, env="live")

    # Live equity/balance -- read-only, no orders. Falls back gracefully
    # (shows "—") if the token/connection isn't available this cycle.
    equity_sek = None
    try:
        import saxo_client
        bal = saxo_client.get_balances(env="live")
        equity_sek = float(bal.get("TotalValue") or 0) or None
    except Exception:
        pass

    W_TOTAL = 139
    HR = f"  {fd.DM}{'─' * W_TOTAL}{fd.W}"
    L = []

    src_tag = "SAXO LIVE" if price_src == "saxo" else "n/a (token expired)"
    L.append(f"  {fd.BD}{fd.GR}╔{'═'*W_TOTAL}╗{fd.W}")
    L.append(f"  {fd.BD}{fd.GR}║{'  FOREX LIVE ACCOUNT — REAL MONEY':^{W_TOTAL}}║{fd.W}")
    equity_s = f"{equity_sek:,.0f} SEK" if equity_sek is not None else "—"
    L.append(f"  {fd.BD}{fd.GR}║{f'  3 Strategies  |  34 Core Pairs  |  Equity: {equity_s}  |  Prices: {src_tag}  |  {now_ts}':^{W_TOTAL}}║{fd.W}")
    L.append(f"  {fd.BD}{fd.GR}╚{'═'*W_TOTAL}╝{fd.W}")
    L.append("")
    L.append(f"  {fd.BD}STRATEGIES{fd.W}   "
             f"{fd.GR}{fd.BD}■ Donchian(30){fd.W}  breakout   "
             f"{fd.CY}{fd.BD}■ EMA{fd.W}  trend   "
             f"{fd.MG}{fd.BD}■ RSI(2){fd.W}  pullback")
    L.append(f"  {fd.DM}This is the REAL-MONEY account -- separate state/orders/P&L from SIM. "
             f"SIM's own dashboard (forex_dashboard.py) is unaffected by anything shown here.{fd.W}")
    L.append(HR)
    L.append("")

    # ── Real account equity (pooled Saxo AccountGroup -- peak / drawdown /
    # return / give-back, NOT the sizing cap). account_equity.py.
    try:
        import account_equity
        L.append(account_equity.render(color=True))
        L.append("")
        L.append(HR)
        L.append("")
    except Exception as exc:
        L.append(f"  {fd.DM}[account equity block unavailable: {exc}]{fd.W}")
        L.append("")

    # ── Open positions (single section -- no core/exotic split needed, this
    # account IS the core universe by construction). forex_dashboard's own
    # _positions_section() can't be reused here: its P&L math is hardwired
    # to _eur_per_unit (SIM's EUR-denominated account), which would silently
    # mis-convert every pair's P&L for this SEK-denominated account -- same
    # layout, rebuilt from scratch below with _sek_per_unit instead.
    total_pnl_sek = 0.0
    total_cost_sek = 0.0
    for p in positions:
        sym, now_px, ep, qty = p["symbol"], live.get(p["symbol"]), p["entry"], p["qty"]
        if now_px and ep > 0:
            quote_ccy = sym[3:6] if len(sym) >= 6 else ""
            rate = _sek_per_unit(quote_ccy, live)
            if rate is not None:
                raw = (now_px - ep) if p["direction"] == "Buy" else (ep - now_px)
                total_pnl_sek  += raw * qty * rate
                total_cost_sek += ep * qty * rate

    L.append(f"  {fd.BD}OPEN POSITIONS{fd.W}  {fd.DM}({len(positions)} active){fd.W}")
    L.append("")
    L.append(
        f"  {fd.DM}"
        f"{'Strategy':<10}  {'Pair':<7}  {'Side':<6}  "
        f"{'Qty':>12}  {'Entry':>10}  {'Now':>10}  "
        f"{'Stop':>10}  {'P&L (SEK)':>12}  {'%':>9}  "
        f"{'Days':>5}  {'Stop Risk':>12}{fd.W}"
    )
    L.append(HR)
    if positions:
        # 2026-08-27/28: LIVE_ALLOWED_STRATEGIES changed {donchian,ema,rsi}
        # -> {bb,rsi} (via a brief {bb,rsi,pullback} step) -> {bb} once rsi
        # moved to the LIVE EUR account in the two-account HIGH_VOLUME
        # split -- current allowlist listed first, but donchian/ema/rsi/
        # pullback still appended if any has a real open position (existing
        # positions from before a change keep trading out normally; they
        # must never just vanish from this dashboard).
        grouped: dict = {}
        for p in positions:
            grouped.setdefault(p["strategy"], []).append(p)
        _current_live = tuple(runner.LIVE_ALLOWED_STRATEGIES)
        strat_order = list(_current_live) + sorted(s for s in grouped if s not in _current_live)
        first_group = True
        for strat in strat_order:
            grp = grouped.get(strat, [])
            if not grp:
                continue
            sc = fd.STRAT_COL.get(strat, fd.DM)
            if not first_group:
                L.append(f"  {fd.DM}{'·'*W_TOTAL}{fd.W}")
            first_group = False
            for p in grp:
                sym, is_long = p["symbol"], p["direction"] == "Buy"
                now_px, ep, stop_px, qty = live.get(sym), p["entry"], p["stop"], p["qty"]
                try:
                    held = (date.today() - date.fromisoformat(p["entry_date"])).days
                except Exception:
                    held = 0
                side_tag = f"{fd.GR}{fd.BD}LONG {fd.W}" if is_long else f"{fd.RD}{fd.BD}SHORT{fd.W}"
                quote_ccy = sym[3:6] if len(sym) >= 6 else ""
                rate = _sek_per_unit(quote_ccy, live) if now_px and ep > 0 else None
                if now_px and ep > 0 and rate is not None:
                    raw_pnl = (now_px - ep) if is_long else (ep - now_px)
                    pnl_sek = raw_pnl * qty * rate
                    pnl_pct = raw_pnl / ep * 100
                    pc = fd.GR if pnl_sek >= 0 else fd.RD
                    pnl_s = f"{pc}{pnl_sek:>+,.0f}{fd.W}"
                    pct_s = f"{pc}{pnl_pct:>+.4f}%{fd.W}"
                    now_s = f"{now_px:.5f}"
                else:
                    pnl_s, pct_s, now_s = f"{fd.DM}{'—':>12}{fd.W}", f"{fd.DM}{'—':>9}{fd.W}", f"{'—':>10}"
                near = (stop_px > 0 and now_px and
                        ((is_long and now_px < stop_px * 1.005) or
                         (not is_long and now_px > stop_px * 0.995)))
                stp_col = f"{fd.RD}{fd.BD}" if near else fd.DM
                if stop_px > 0 and now_px:
                    dist_s = f"{fd.DM}{abs(now_px - stop_px) / now_px * 100:.2f}% from stop{fd.W}"
                else:
                    dist_s = f"{fd.DM}{'—':>12}{fd.W}"
                L.append(
                    f"  {sc}{fd.BD}{strat:<10}{fd.W}  {fd.BD}{sym:<7}{fd.W}  {side_tag}  "
                    f"{fd.DM}{qty:>12,}{fd.W}  {fd.DM}{ep:>10.5f}{fd.W}  {fd.BD}{now_s:>10}{fd.W}  "
                    f"{stp_col}{stop_px:>10.5f}{fd.W}  {fd._pad_ansi(pnl_s, 17)}  "
                    f"{fd._pad_ansi(pct_s, 14)}  {fd.DM}{held:>5}d{fd.W}  {dist_s}"
                )
        L.append(HR)
        tc = fd.GR if total_pnl_sek >= 0 else fd.RD
        tpct = (total_pnl_sek / total_cost_sek * 100) if total_cost_sek > 0 else 0
        L.append(
            f"  {fd.BD}TOTAL{fd.W}  "
            f"{fd.DM}{len(positions)} positions  |  Cost: {total_cost_sek:>,.0f} SEK  |  "
            f"Unrealized P&L: {fd.W}{tc}{fd.BD}{total_pnl_sek:>+,.0f} SEK  ({tpct:>+.4f}%){fd.W}"
        )
    else:
        L.append(f"  {fd.DM}No open live positions.{fd.W}")
    L.append(HR)
    L.append("")

    # ── Strategy breakdown -- single table, module="forex_live" ───────────
    # Driven off runner.LIVE_ALLOWED_STRATEGIES itself (currently just
    # {"bb"} as of the 2026-08-28 two-account split -- rsi moved to the
    # LIVE EUR account) rather than a hardcoded count, so this exclude set
    # tracks the allowlist automatically whenever it changes.
    _not_live = set(runner.STRATEGIES) - runner.LIVE_ALLOWED_STRATEGIES
    L.extend(fd._strategy_breakdown_table(
        "STRATEGY BREAKDOWN — LIVE (real money)",
        positions, live, symbols=HIGH_VOLUME_SYMBOLS, universe_size=len(HIGH_VOLUME_SYMBOLS),
        exclude=_not_live, color=fd.GR, total_label="LIVE TOTAL",
        module="forex_live", currency_label="SEK"))

    L.append(f"  {fd.DM}Realized/Today P&L above is computed the same way as SIM's dashboard "
             f"but in SEK, via pnl_tracker module='forex_live' (fully separate ledger from SIM's 'forex').{fd.W}")
    L.append("")

    # ── Per-pair performance -- every HIGH_VOLUME pair with at least one
    # closed LIVE trade, not just a strategy-level aggregate. Scandi-cross
    # core pairs (EURNOK/EURSEK/USDNOK/USDSEK/USDDKK) are marked with a
    # flag and tallied separately so their progress toward the 10-trade
    # review checkpoint is visible at a glance instead of requiring a
    # manual count.
    pair_stats = pnl_tracker.get_pair_summary("forex_live")
    L.append(f"  {fd.BD}PER-PAIR PERFORMANCE{fd.W}  {fd.DM}(every HIGH_VOLUME pair with a closed LIVE trade){fd.W}")
    L.append("")
    if pair_stats:
        L.append(
            f"  {fd.DM}{'Pair':<9}  {'Closed':>6}  {'Open':>4}  {'W/L':>7}  "
            f"{'WR%':>6}  {'PF':>6}  {'Total P&L (SEK)':>16}  {'Best':>10}  {'Worst':>10}{fd.W}"
        )
        L.append(HR)
        scandi_trades = scandi_wins = 0
        scandi_pnl = 0.0
        for r in pair_stats:
            is_scandi = r["symbol"] in SCANDI_CORE_PAIRS
            flag = f"{fd.YL}⚑{fd.W}" if is_scandi else " "
            pc = fd.GR if r["total_pnl"] >= 0 else fd.RD
            pf_s = f"{r['profit_factor']:.2f}" if r["profit_factor"] is not None else "∞" if r["losses"] == 0 else "—"
            L.append(
                f"  {flag} {fd.BD}{r['symbol']:<7}{fd.W}  {r['trades']:>6}  {r['open']:>4}  "
                f"{r['wins']}W/{r['losses']}L{'':>1}  {r['win_rate']:>5.1f}%  {pf_s:>6}  "
                f"{pc}{r['total_pnl']:>+16,.0f}{fd.W}  {fd.GR}{r['best']:>+10,.0f}{fd.W}  {fd.RD}{r['worst']:>+10,.0f}{fd.W}"
            )
            if is_scandi:
                scandi_trades += r["trades"]
                scandi_wins   += r["wins"]
                scandi_pnl    += r["total_pnl"]
        L.append(HR)
        remaining = max(0, SCANDI_REVIEW_TRADE_COUNT - scandi_trades)
        sc = fd.GR if scandi_pnl >= 0 else fd.RD
        status = (f"{fd.YL}{fd.BD}review checkpoint reached — decide keep/remove{fd.W}" if remaining == 0
                  else f"{fd.DM}{remaining} more to go{fd.W}")
        L.append(
            f"  {fd.YL}⚑{fd.W} {fd.BD}SCANDI-CORE{fd.W}  {fd.DM}(EURNOK/EURSEK/USDNOK/USDSEK/USDDKK){fd.W}  "
            f"{scandi_trades}/{SCANDI_REVIEW_TRADE_COUNT} closed trades toward review  |  "
            f"{scandi_wins}W/{scandi_trades - scandi_wins}L  |  {sc}{scandi_pnl:>+,.0f} SEK{fd.W}  |  {status}"
        )
    else:
        L.append(f"  {fd.DM}No closed LIVE trades yet on any pair.{fd.W}")
    L.append(HR)
    L.append("")

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
        sys.stdout.flush()
        return

    while True:
        try:
            out = _render(interval=interval)
            fd._clear_console()
            sys.stdout.write(out)
            sys.stdout.flush()
            time.sleep(interval)
        except KeyboardInterrupt:
            sys.stdout.write(f"\n{fd.W}")
            break


if __name__ == "__main__":
    main()
