"""
ai_dashboard.py  —  AI SIM TWIN vs the deterministic SIM books
-------------------------------------------------------------
The forward A/B: for both forex and stocks, the AI-decision paper twin
(forex/runner.py --account ai_sim  ·  atos_ai_stocks.py) next to its
deterministic SIM counterpart. Same signals, same period — the only
variable is the AI layer (Copilot resize/skip for forex; the basket-ranker's
re-ranked pick for stocks).

Usage:
    python ai_dashboard.py --once
    python ai_dashboard.py            # refresh 30s
    python ai_dashboard.py --fast     # 5s
"""

import json
import os
import re as _re
import sqlite3
import sys
import time
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

try:
    import ctypes as _ct
    _k = _ct.windll.kernel32; _h = _k.GetStdHandle(-11); _m = _ct.c_ulong()
    _k.GetConsoleMode(_h, _ct.byref(_m)); _k.SetConsoleMode(_h, _m.value | 0x0004)
    _VT = True
except Exception:
    _VT = False
_ANSI = _re.compile(r"\033\[[0-9;]*m")

GR = "\033[92m"; RD = "\033[91m"; YL = "\033[93m"; CY = "\033[96m"; BL = "\033[94m"
W = "\033[0m"; BD = "\033[1m"; DM = "\033[2m"

import pnl_tracker as PT

AI_STOCKS_DB   = os.path.join(BASE, "data", "atos_ai.db")
DET_STOCKS_DB  = os.path.join(BASE, "data", "atos_live.db")
SHADOW_DEC     = os.path.join(BASE, "data", "ai_shadow_decisions.jsonl")
BASKET_SHADOW  = os.path.join(BASE, "data", "ai_basket_shadow.jsonl")
AI_STOCKS_STATUS = os.path.join(BASE, "data", "atos_ai_stocks_status.json")


def _tail_jsonl(path, n=400):
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


def _fx_line(mod: str) -> dict:
    """Aggregate closed-trade stats for a forex pnl_tracker module."""
    rows = PT.get_strategy_summary(mod)
    n = sum(r["trades"] for r in rows)
    pnl = round(sum(r["total_pnl"] for r in rows), 1)
    w = sum(r["wins"] for r in rows)
    gp = sum(r.get("gross_profit", 0.0) for r in rows)
    gl = sum(abs(r.get("gross_loss", 0.0)) for r in rows)
    openn = sum(r.get("open", 0) for r in rows)
    return {"closed": n, "pnl": pnl, "wr": round(w / n * 100, 1) if n else 0.0,
            "pf": round(gp / gl, 2) if gl else None, "open": openn, "by": rows}


def _stock_line(dbp: str) -> dict:
    if not os.path.exists(dbp):
        return {"closed": 0, "pnl": 0.0, "wr": 0.0, "pf": None, "open": 0, "holds": {}}
    c = sqlite3.connect(dbp); c.row_factory = sqlite3.Row
    try:
        cl = [dict(r) for r in c.execute(
            "select pnl_sek, exit_reason from trades where exit_price is not null and strategy='US Blend'")]
        op = [dict(r) for r in c.execute(
            "select ticker, shares from trades where exit_price is null and strategy='US Blend'")]
    except Exception:
        cl, op = [], []
    finally:
        c.close()
    pnls = [t["pnl_sek"] or 0 for t in cl]
    w = sum(1 for p in pnls if p > 0)
    gp = sum(p for p in pnls if p > 0); gl = -sum(p for p in pnls if p < 0)
    return {"closed": len(cl), "pnl": round(sum(pnls), 0),
            "wr": round(w / len(cl) * 100, 1) if cl else 0.0,
            "pf": round(gp / gl, 2) if gl else None, "open": len(op),
            "holds": {t["ticker"]: t["shares"] for t in op}}


def _pf(x) -> str:
    return f"{x:.2f}" if x else "—"


def _verdict(ai_pnl, det_pnl, ai_closed, det_closed) -> str:
    if ai_closed == 0 or det_closed == 0:
        return f"{DM}not enough closed trades yet (need both books trading){W}"
    d = ai_pnl - det_pnl
    if abs(d) < 1:
        return f"{DM}dead heat{W}"
    return (f"{GR}AI ahead by {d:+,.0f}{W}" if d > 0 else f"{RD}AI behind by {d:+,.0f}{W}")


def render() -> str:
    L = []
    L.append(f"{BD}{'='*74}{W}")
    L.append(f"{BD}  ATOS AI SIM TWIN — forward A/B vs the deterministic SIM books{W}")
    L.append(f"{DM}  {datetime.now():%Y-%m-%d %H:%M:%S} PKT   ·   both books paper, same signals{W}")
    L.append(f"{BD}{'='*74}{W}")

    # ── FOREX ────────────────────────────────────────────────────────────
    det = _fx_line("forex")
    ai  = _fx_line("forex_ai")
    L.append("")
    L.append(f"{BD}  FOREX{W}   {DM}(deterministic SIM book vs --account ai_sim, Copilot resize/skip applied){W}")
    L.append(f"  {DM}{'':12}  {'Closed':>7}  {'WR%':>6}  {'PF':>6}  {'P&L (EUR)':>12}  {'Open':>5}{W}")
    L.append(f"  {'deterministic':12}  {det['closed']:>7}  {det['wr']:>6.1f}  "
             f"{_pf(det['pf']):>6}  {det['pnl']:>12,.0f}  {det['open']:>5}")
    L.append(f"  {CY}{'AI twin':12}{W}  {ai['closed']:>7}  {ai['wr']:>6.1f}  "
             f"{_pf(ai['pf']):>6}  {ai['pnl']:>12,.0f}  {ai['open']:>5}")
    L.append(f"  {DM}verdict:{W} {_verdict(ai['pnl'], det['pnl'], ai['closed'], det['closed'])}")

    dec = [d for d in _tail_jsonl(SHADOW_DEC) if d.get("account_env") == "ai_sim"]
    if dec:
        from collections import Counter
        acts = Counter((d.get("decision") or {}).get("action") or d.get("action") for d in dec)
        applied = sum(1 for d in dec if d.get("applied"))
        L.append(f"  {DM}copilot on the twin: {dict(acts)} · {applied} applied to sizing · {len(dec)} total{W}")

    # ── STOCKS ───────────────────────────────────────────────────────────
    dets = _stock_line(DET_STOCKS_DB)
    ais  = _stock_line(AI_STOCKS_DB)
    L.append("")
    L.append(f"{BD}  STOCKS — US Blend{W}   {DM}(deterministic top-N vs the basket-ranker's re-ranked pick){W}")
    L.append(f"  {DM}{'':12}  {'Closed':>7}  {'WR%':>6}  {'PF':>6}  {'P&L (SEK)':>12}  {'Open':>5}{W}")
    L.append(f"  {'deterministic':12}  {dets['closed']:>7}  {dets['wr']:>6.1f}  "
             f"{_pf(dets['pf']):>6}  {dets['pnl']:>12,.0f}  {dets['open']:>5}")
    L.append(f"  {CY}{'AI twin':12}{W}  {ais['closed']:>7}  {ais['wr']:>6.1f}  "
             f"{_pf(ais['pf']):>6}  {ais['pnl']:>12,.0f}  {ais['open']:>5}")
    L.append(f"  {DM}verdict:{W} {_verdict(ais['pnl'], dets['pnl'], ais['closed'], dets['closed'])}")

    _hd, _ha = set(dets["holds"]), set(ais["holds"])
    if _hd or _ha:
        drop = _hd - _ha; add = _ha - _hd
        L.append(f"  {DM}holdings: deterministic {sorted(_hd)}{W}")
        L.append(f"  {DM}          AI twin       {sorted(_ha)}"
                 + (f"  {RD}dropped {sorted(drop)}{W}" if drop else "")
                 + (f"  {GR}only-AI {sorted(add)}{W}" if add else "") + f"{W}")

    bk = [r for r in _tail_jsonl(BASKET_SHADOW) if r.get("account_env") == "ai_sim"][-3:]
    if bk:
        L.append(f"  {DM}last basket calls:{W}")
        for r in bk:
            ch = f"{YL}re-ranked{W}" if r.get("changed") else f"{DM}kept{W}"
            L.append(f"    {DM}{str(r.get('as_of_date',''))}{W}  {r.get('det_offense')} → "
                     f"{r.get('ai_offense')}  {ch}  {DM}{(r.get('reasoning') or '')[:70]}{W}")

    L.append("")
    L.append(f"{DM}  Both twins are SIM paper. If the AI book is clearly ahead through a rough{W}")
    L.append(f"{DM}  patch, that is the evidence for the M5 review / the future full-autonomy plan.{W}")
    return "\n".join(L)


def _emit(s: str) -> None:
    if not _VT or not sys.stdout.isatty():
        s = _ANSI.sub("", s)
    print(s)


def main():
    once = "--once" in sys.argv
    fast = "--fast" in sys.argv
    while True:
        out = render()
        if once:
            _emit(out); return
        os.system("cls" if os.name == "nt" else "clear")
        _emit(out)
        time.sleep(5 if fast else 30)


if __name__ == "__main__":
    main()
