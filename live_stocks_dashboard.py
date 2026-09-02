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


def _status() -> dict:
    try:
        return json.load(open(STATUS_FILE, encoding="utf-8")) if os.path.exists(STATUS_FILE) else {}
    except Exception:
        return {}


def render() -> str:
    cap = CAP.stocks_live_risk_equity_sek()
    L = []
    L.append(f"{BD}{'='*70}{W}")
    L.append(f"{BD}  ATOS LIVE STOCKS — US Blend sleeve{W}   REAL MONEY")
    L.append(f"{YL}  OBSERVE-ONLY — no real orders (Phase 1){W}")
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

        # ── Scan signals — the orders THIS scan produced (would-be in Phase 1) ─
        acts = st.get("actions") or []
        L.append("")
        L.append(f"{BD}  SCAN SIGNALS{W}  {DM}({len(acts)} this scan — "
                 f"{st.get('buy',0)} buy / {st.get('sell',0)} sell){W}")
        if not acts:
            L.append(f"{DM}    holdings already match target — no orders this scan{W}")
        else:
            L.append(f"    {DM}{'Action':<8} {'Ticker':<7} {'Shares':>6} {'Price':>9}  Reason{W}")
            for a in acts:
                act = a.get("action", "")
                acol = GR + BD if act == "BUY" else (CY if act in ("SELL", "EXIT") else DM)
                wb = f" {DM}(would-be){W}" if a.get("would_be") else ""
                L.append(f"    {acol}{act:<8}{W} {BD}{(a.get('ticker') or '')[:7]:<7}{W} "
                         f"{a.get('shares',0):>6} {(a.get('price') or 0):>9.2f}  "
                         f"{DM}{(a.get('reason') or '')[:34]}{W}{wb}")

    openp = _rows("select * from trades where exit_price is null and strategy='US Blend' order by entry_date")
    L.append("")
    L.append(f"{BD}  OPEN POSITIONS ({len(openp)}){W}")
    if not openp:
        L.append(f"{DM}    none — the sleeve holds no real stock positions yet{W}")
    else:
        for t in openp:
            L.append(f"    {t.get('ticker',''):<8} {t.get('shares',0):>6} sh  "
                     f"entry ${t.get('entry_price',0):.2f}  stop ${t.get('stop_price',0):.2f}")

    closed = _rows("select * from trades where exit_price is not null and strategy='US Blend' "
                   "order by exit_date desc limit 10")
    if closed:
        L.append("")
        L.append(f"{BD}  RECENT CLOSED ({len(closed)}){W}")
        for t in closed:
            pnl = t.get("pnl_sek") or 0
            col = GR if pnl >= 0 else RD
            L.append(f"    {t.get('ticker',''):<8} {col}{pnl:+,.0f} SEK{W}  {DM}{t.get('exit_reason','')}{W}")

    wb = _tail_jsonl(WOULD_BE_ORDERS, 12)
    L.append("")
    L.append(f"{BD}  WOULD-BE ORDERS — full history{W}  {DM}(last {len(wb)}, all scans){W}")
    if not wb:
        L.append(f"{DM}    none logged yet{W}")
    for r in wb:
        L.append(f"    {DM}{str(r.get('ts',''))[:16]}{W}  {r.get('side',''):<4} "
                 f"{r.get('shares',0):>5} {r.get('ticker',''):<7} @ ${r.get('price_usd',0):.2f}  "
                 f"~{r.get('notional_sek',0):,.0f} SEK")

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
