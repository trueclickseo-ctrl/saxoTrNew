"""
live_stocks_dashboard.py  —  ATOS LIVE STOCKS (US Blend) sleeve
--------------------------------------------------------------
Real-money US Blend stocks sleeve view. SEPARATE from stocks_dashboard.py
(SIM) and forex_live_dashboard.py (LIVE forex).

Equity base = config/capital.json strategies.stocks_live.risk_equity_sek (30k).
The pooled Saxo balance is shown LABELLED "pooled / shared with forex LIVE" --
Saxo cannot split /balances/me per sub-account in a shared margin group.

Phase 1 banner: OBSERVE-ONLY — no real orders.

Usage:
    python live_stocks_dashboard.py --once     # print once and exit
    python live_stocks_dashboard.py            # refresh every 30s
    python live_stocks_dashboard.py --fast     # refresh every 5s
"""

import os
import sys
import json
import re as _re
import sqlite3
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Enable ANSI/VT colour processing on Windows consoles that don't have it on
# (older conhost, some PowerShell hosts) -- otherwise every colour code prints
# as a literal "<-[1m". Same shim as stocks_dashboard.py.
try:
    import ctypes as _ct
    _k32 = _ct.windll.kernel32
    _h = _k32.GetStdHandle(-11)
    _mode = _ct.c_ulong()
    _k32.GetConsoleMode(_h, _ct.byref(_mode))
    _k32.SetConsoleMode(_h, _mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    _VT_OK = True
except Exception:
    _VT_OK = False

_ANSI_RE = _re.compile(r"\033\[[0-9;]*m")

DB_PATH          = os.path.join(BASE_DIR, "data", "atos_live_stocks.db")
WOULD_BE_ORDERS  = os.path.join(BASE_DIR, "data", "us_blend_live_would_be_orders.jsonl")
BASKET_SHADOW    = os.path.join(BASE_DIR, "data", "ai_basket_shadow.jsonl")
STATUS_FILE      = os.path.join(BASE_DIR, "data", "stocks_live_status.json")

import atos.capital_config as CAP

GR = "\033[92m"; RD = "\033[91m"; YL = "\033[93m"; CY = "\033[96m"; BL = "\033[94m"
W = "\033[0m"; BD = "\033[1m"; DM = "\033[2m"


def _rows(sql, params=()):
    if not os.path.exists(DB_PATH):
        return []
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    except Exception:
        return []
    finally:
        con.close()


def _tail_jsonl(path, n=12):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if ln:
                try:
                    out.append(json.loads(ln))
                except Exception:
                    pass
    return out[-n:]


def _pooled_balance():
    try:
        import saxo_client
        b = saxo_client.get_balances(env="live")
        return b.get("TotalValue"), b.get("InitialMargin", {}).get("MarginUtilizationPct")
    except Exception:
        return None, None


def _live_prices_from_saxo() -> dict:
    """Return {ticker: current_price_usd} from LIVE Saxo positions endpoint."""
    try:
        import saxo_client
        data = saxo_client.get_positions(env="live")
        out = {}
        for item in data.get("Data", []):
            base  = item.get("PositionBase", {})
            view  = item.get("PositionView", {})
            disp  = item.get("DisplayAndFormat", {})
            sym   = (disp.get("Symbol") or base.get("Symbol") or "").split(".")[0].upper()
            price = view.get("CurrentPrice") or base.get("CurrentPrice") or 0
            if sym and price:
                out[sym] = float(price)
        return out
    except Exception:
        return {}


_PNL_RESET_PATH = os.path.join(BASE_DIR, "data", "pnl_reset.json")

def _load_stock_cutoffs() -> dict:
    """Returns {strategy: 'YYYY-MM-DD'} from pnl_reset.json[stocks]."""
    try:
        if os.path.exists(_PNL_RESET_PATH):
            return json.load(open(_PNL_RESET_PATH, encoding="utf-8")).get("stocks") or {}
    except Exception:
        pass
    return {}

def _db_stats() -> dict:
    """Per-strategy stats + '_total' rollup.
    Open positions: unfiltered. Closed stats: filtered by entry_date >= cutoff
    from pnl_reset.json[stocks] to exclude old-era artifacts."""
    if not os.path.exists(DB_PATH):
        return {}
    try:
        cutoffs = _load_stock_cutoffs()
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row

        open_rows = conn.execute(
            "SELECT strategy, COUNT(*) AS n FROM trades WHERE exit_price IS NULL GROUP BY strategy"
        ).fetchall()
        opens = {(r["strategy"] or "Unknown"): int(r["n"]) for r in open_rows}

        if cutoffs:
            parts, params = [], []
            covered = list(cutoffs.keys())
            for strat, dt in cutoffs.items():
                parts.append("(strategy = ? AND entry_date >= ?)")
                params.extend([strat, dt])
            ph = ",".join("?" * len(covered))
            parts.append(f"strategy NOT IN ({ph})")
            params.extend(covered)
            extra_where = " AND (" + " OR ".join(parts) + ")"
        else:
            extra_where, params = "", []

        closed_rows = conn.execute(f"""
            SELECT
                strategy,
                COUNT(*)                                         AS closed,
                SUM(pnl_sek)                                     AS realized,
                COUNT(*) FILTER (WHERE pnl_sek > 0)             AS wins,
                COUNT(*) FILTER (WHERE pnl_sek <= 0)            AS losses,
                SUM(pnl_sek) FILTER (WHERE pnl_sek > 0)         AS gross_win,
                ABS(SUM(pnl_sek) FILTER (WHERE pnl_sek <= 0))   AS gross_loss
            FROM trades WHERE exit_price IS NOT NULL {extra_where}
            GROUP BY strategy""", params).fetchall()
        conn.close()

        out = {}
        totals = {"open": 0, "closed": 0, "wins": 0, "losses": 0,
                  "realized": 0.0, "gross_win": 0.0, "gross_loss": 0.0}
        all_strats = set(opens) | {(r["strategy"] or "Unknown") for r in closed_rows}
        for strat in all_strats:
            out[strat] = {"open": opens.get(strat, 0), "closed": 0, "wins": 0,
                          "losses": 0, "realized": 0.0, "gross_win": 0.0,
                          "gross_loss": 0.0, "since": cutoffs.get(strat)}
        for r in closed_rows:
            strat = r["strategy"] or "Unknown"
            for k in ("closed", "wins", "losses", "realized", "gross_win", "gross_loss"):
                out[strat][k] = float(r[k] or 0)
        for strat, d in out.items():
            for k in totals:
                totals[k] += d.get(k) or 0
        out["_total"] = totals
        return out
    except Exception:
        return {}


def _status() -> dict:
    try:
        return json.load(open(STATUS_FILE, encoding="utf-8")) if os.path.exists(STATUS_FILE) else {}
    except Exception:
        return {}


def render() -> str:
    cap = CAP.stocks_live_risk_equity_sek()
    L = []
    L.append(f"{BD}{'='*70}{W}")
    L.append(f"{BD}  ATOS LIVE STOCKS — US Blend sleeve{W}   {RD}REAL MONEY{W}")
    L.append(f"{DM}  {datetime.now():%Y-%m-%d %H:%M:%S} PKT{W}")
    L.append(f"{BD}{'='*70}{W}")

    L.append(f"  Capital cap (this sleeve) : {BD}{cap:,.0f} SEK{W}")
    pooled, util = _pooled_balance()
    if pooled is not None:
        L.append(f"  Saxo balance             : {pooled:,.0f} SEK  "
                 f"{DM}(pooled / shared with forex LIVE){W}")
    if util is not None:
        col = RD if util >= 50 else GR
        L.append(f"  Pooled margin utilization : {col}{util:.1f}%{W}  {DM}(50% entry gate){W}")

    # ── Open positions — top of dashboard ────────────────────────────────────
    openp      = _rows("select * from trades where exit_price is null order by entry_date")
    live_px    = _live_prices_from_saxo()
    today      = datetime.now().date()
    L.append("")
    L.append(f"{BD}  OPEN POSITIONS ({len(openp)}){W}")
    if not openp:
        L.append(f"{DM}    none — the sleeve holds no real stock positions yet{W}")
    else:
        HDR = (f"  {DM}{'Ticker':<7} {'Shrs':>4}  {'Entry':>7}  {'Now':>7}  "
               f"{'Stop':>7}  {'P&L USD':>9}  {'Chg%':>6}  "
               f"{'Strategy':<12}  {'Exit Trigger':<18}  {'Regime':<12}  {'Day':>3}{W}")
        SEP = f"  {DM}{'─'*106}{W}"
        L.append(HDR)
        L.append(SEP)
        for t in openp:
            ticker  = t.get("ticker", "")
            shrs    = t.get("shares", 0) or 0
            entry   = t.get("entry_price", 0) or 0
            stop    = t.get("stop_price", 0) or 0
            tsh     = t.get("trailing_stop_high", entry) or entry
            regime  = (t.get("regime_at_entry") or "—").replace("_", " ")
            strat   = (t.get("strategy") or "US Blend").replace("US ", "")
            ed      = t.get("entry_date", "")
            # momentum day
            try:
                from datetime import date as _date
                ed_date = _date.fromisoformat(str(ed)[:10])
                mom_day = (today - ed_date).days + 1
            except Exception:
                mom_day = 0
            # live price (fall back to trailing_stop_high reference if unavailable)
            now_px = live_px.get(ticker.upper(), 0)
            if now_px > 0:
                pnl_usd = (now_px - entry) * shrs
                chg_pct = (now_px - entry) / entry * 100 if entry else 0
            else:
                pnl_usd = 0.0
                chg_pct = 0.0
            # exit trigger
            if tsh > entry * 1.001:
                exit_trig = f"trail >${tsh:.2f}"
            else:
                exit_trig = f"stop ${stop:.2f}"
            if mom_day >= 14:
                exit_trig = "time (14d)"
            pnl_col = GR if pnl_usd >= 0 else RD
            chg_col = GR if chg_pct >= 0 else RD
            now_str = f"${now_px:.2f}" if now_px else "  —   "
            L.append(
                f"  {BD}{ticker:<7}{W} {shrs:>4}  ${entry:>6.2f}  {now_str:>7}  "
                f"${stop:>6.2f}  "
                f"{pnl_col}{pnl_usd:>+9.2f}{W}  "
                f"{chg_col}{chg_pct:>+5.1f}%{W}  "
                f"{DM}{strat:<12}{W}  {DM}{exit_trig:<18}{W}  "
                f"{DM}{regime:<12}{W}  {DM}{mom_day:>3}d{W}"
            )
        L.append(SEP)

    # ── Last scan: blend target basket (the "signal") ──────────────────────
    st = _status()
    if st:
        ts = str(st.get("timestamp", ""))[:16].replace("T", " ")
        mode = f"{YL}OBSERVE{W}" if st.get("dry_run") else f"{RD}LIVE{W}"
        eo = f"  {DM}exits-only{W}" if st.get("exits_only") else ""
        L.append("")
        L.append(f"{BD}  LAST SCAN{W}  {DM}{ts} PKT{W}   [{mode}]{eo}   "
                 f"{DM}budget {st.get('budget_sek') or 0:,.0f} SEK{W}")
        sig = st.get("signal") or {}
        if sig.get("risk_off"):
            L.append(f"    {RD}{BD}RISK-OFF{W}  {DM}{sig.get('reason','')}{W}  — target = cash, no new buys")
        else:
            tgts = sig.get("targets") or []
            mom  = sig.get("momentum") or []
            lv   = sig.get("lowvol") or []
            L.append(f"    signal: {DM}{sig.get('reason','')}{W}")
            L.append(f"    {CY}offense (momentum){W} : {' '.join(mom) if mom else '—'}")
            L.append(f"    {BL}defense (low-vol){W}  : {' '.join(lv) if lv else '—'}")
            L.append(f"    {BD}target basket{W}     : {' '.join(tgts) if tgts else '—'}")

        # ── Rebalance clocks — LIVE vs SIM (independent 14-day cycles) ─────────
        bs = st.get("book_state") or {}
        if bs:
            L.append("")
            L.append(f"{BD}  REBALANCE CLOCKS{W}  {DM}(the two books run on independent 14-day cycles){W}")
            for label, key in (("LIVE", "live_stocks"), ("SIM ", "sim")):
                b = bs.get(key) or {}
                last = b.get("last_rebalance") or "never"
                ds = b.get("days_since")
                due = b.get("next_due_in_days")
                when = (f"{ds}d ago, next in {due}d" if ds is not None else "first run pending")
                hold = b.get("holdings") or {}
                hs = " ".join(f"{t}×{n}" for t, n in sorted(hold.items())) or "—"
                L.append(f"    {label}  last {last}  {DM}({when}){W}")
                L.append(f"          holds: {DM}{hs}{W}")

        # ── Today's scan signals — same layout as the SIM stocks dashboard ────
        acts = st.get("actions") or []
        HR = f"{DM}  {'─' * 96}{W}"
        L.append("")
        L.append(f"{BD}  TODAY'S SCAN SIGNALS{W}  {DM}(scan: {ts}){W}"
                 + (f"  {YL}[would-be — observe only]{W}" if st.get("dry_run") else ""))
        L.append("")
        if not acts:
            L.append(f"{DM}    holdings already match the target basket — no orders this scan{W}")
        else:
            L.append(f"  {DM}{'Action':<10}  {'Ticker':<7}  {'Strategy':<14}  "
                     f"{'Score':>5}  {'Shares':>6}  {'Price':>8}  Reason{W}")
            L.append(HR)
            n_buy = n_exit = n_blocked = 0
            for a in acts:
                raw = a.get("action", "")
                disp = "BUY" if raw == "BUY" else ("EXIT" if raw in ("SELL", "EXIT") else raw or "—")
                if disp == "BUY":      n_buy += 1
                elif disp == "EXIT":   n_exit += 1
                elif disp == "BLOCKED": n_blocked += 1
                acol = {"BUY": GR + BD, "EXIT": CY, "BLOCKED": DM}.get(disp, DM)
                L.append(
                    f"  {acol}{disp:<10}{W}  {BD}{(a.get('ticker') or '')[:7]:<7}{W}  "
                    f"{DM}{(a.get('strategy') or 'US Blend')[:14]:<14}{W}  "
                    f"{(a.get('score') or 0):>5.2f}  {a.get('shares', 0):>6}  "
                    f"{(a.get('price') or 0):>8.2f}  {DM}{(a.get('reason') or '')[:40]}{W}"
                )
            L.append(HR)
            L.append(f"  {GR}{n_buy} BUY{W}  {CY}{n_exit} EXIT{W}  {DM}{n_blocked} BLOCKED{W}")

    closed = _rows("select * from trades where exit_price is not null "
                   "order by exit_date desc limit 10")
    if closed:
        L.append("")
        L.append(f"{BD}  RECENT CLOSED ({len(closed)}){W}")
        for t in closed:
            pnl = t.get("pnl_sek") or 0
            col = GR if pnl >= 0 else RD
            L.append(f"    {t.get('ticker',''):<8} {col}{pnl:+,.0f} SEK{W}  "
                     f"{DM}{t.get('strategy','')}  {t.get('exit_reason','')}{W}")

    # ── per-strategy breakdown ────────────────────────────────────────────────
    stats = _db_stats()
    cutoffs = _load_stock_cutoffs()
    if stats:
        HD = f"{DM}  {'-' * 79}{W}"
        L.append("")
        L.append(f"{BD}  STRATEGY BREAKDOWN{W}  "
                 f"{DM}(closed stats since cutoff — open positions always shown){W}")
        L.append(f"  {DM}{'STRATEGY':<24}  {'ACT':>3}  {'CLS':>4}  {'W':>4}  {'L':>4}  "
                 f"{'WR':>7}  {'PF':>7}  {'REALIZED':>14}{W}")
        L.append(HD)
        for strat, d in sorted((k, v) for k, v in stats.items() if k != "_total"):
            wr  = d["wins"] / d["closed"] * 100 if d["closed"] else 0.0
            pf  = d["gross_win"] / d["gross_loss"] if d["gross_loss"] else float("inf")
            pfs = f"{pf:.2f}" if pf != float("inf") else "inf"
            rl  = d["realized"] or 0
            rcol = GR if rl >= 0 else RD
            since = cutoffs.get(strat)
            since_s = f"  {DM}since {since}{W}" if since else ""
            L.append(f"  {strat:<24}  {d['open']:>3}  {d['closed']:>4}  "
                     f"{d['wins']:>4}  {d['losses']:>4}  {wr:>6.1f}%  "
                     f"{pfs:>7}  {rcol}{rl:>+14,.0f} SEK{W}{since_s}")
        L.append(HD)
        t = stats["_total"]
        twr = t["wins"] / t["closed"] * 100 if t["closed"] else 0.0
        tpf = t["gross_win"] / t["gross_loss"] if t["gross_loss"] else float("inf")
        tpfs = f"{tpf:.2f}" if tpf != float("inf") else "inf"
        trl  = t["realized"] or 0
        trcol = GR if trl >= 0 else RD
        L.append(f"  {BD}{'TOTAL':<24}  {t['open']:>3}  {t['closed']:>4}  "
                 f"{t['wins']:>4}  {t['losses']:>4}  {twr:>6.1f}%  "
                 f"{tpfs:>7}  {trcol}{trl:>+14,.0f} SEK{W}")

    wb = _tail_jsonl(WOULD_BE_ORDERS, 15)
    L.append("")
    L.append(f"{BD}  WOULD-BE ORDERS — full history{W}  {DM}(last {len(wb)} across all observe scans){W}")
    if not wb:
        L.append(f"{DM}    none logged yet — first scan runs at 02:40 PKT{W}")
    else:
        L.append(f"  {DM}{'When':<16}  {'Side':<4}  {'Ticker':<7}  {'Shares':>6}  "
                 f"{'Price':>9}  {'Notional':>12}{W}")
        L.append(f"{DM}  {'─' * 66}{W}")
        for r in wb:
            when = str(r.get("ts", "")).replace("T", " ")[:16]
            side = (r.get("side") or "").upper()
            scol = GR if side == "BUY" else CY
            L.append(
                f"  {DM}{when:<16}{W}  {scol}{side:<4}{W}  "
                f"{BD}{(r.get('ticker') or '')[:7]:<7}{W}  {r.get('shares', 0):>6}  "
                f"{DM}${W}{(r.get('price_usd') or 0):>8,.2f}  "
                f"{(r.get('notional_sek') or 0):>9,.0f} SEK"
            )

    basket = [r for r in _tail_jsonl(BASKET_SHADOW, 40)
              if r.get("account_env") == "live_stocks"][-3:]
    if basket:
        L.append("")
        L.append(f"{BD}  AI BASKET-RANKER (shadow, log-only){W}")
        for r in basket:
            ag = r.get("_agent", {})
            L.append(f"    {DM}{str(r.get('as_of_date',''))}{W}  det={r.get('det_offense')}  "
                     f"ai={ag.get('offense') if isinstance(ag, dict) else '?'}")

    L.append("")
    L.append(f"{DM}  Separate module from ATOS LIVE FOREX. Shares only the Saxo SEK login.{W}")
    return "\n".join(L)


def _emit(out: str) -> None:
    # Strip colour codes if the console can't render them or output is redirected.
    if not _VT_OK or not sys.stdout.isatty():
        out = _ANSI_RE.sub("", out)
    print(out)


def main():
    once = "--once" in sys.argv
    fast = "--fast" in sys.argv
    while True:
        out = render()
        if once:
            _emit(out)
            return
        os.system("cls" if os.name == "nt" else "clear")
        _emit(out)
        time.sleep(5 if fast else 30)


if __name__ == "__main__":
    main()
