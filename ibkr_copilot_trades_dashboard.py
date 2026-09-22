"""
ibkr_copilot_trades_dashboard.py
---------------------------------
AI Copilot TRADE PERFORMANCE dashboard.
Shows actual closed-trade WR / PF / P&L for Copilot-influenced trades,
plus shadow-decision stats for accounts still in observation mode.

Sources:
  - data/pnl_ledger.db  module=forex_ai  → Forex Copilot closed trades (ai_sim)
  - data/ibkr_stocks.db                  → IBKR Stocks closed trades (Phase D+)
  - data/ai_shadow_decisions.jsonl        → shadow decisions for all accounts

Phase notes:
  Forex  : Phase B active — ai_sim book applies Copilot resize/skip decisions.
           159+ closed trades in pnl_ledger (module=forex_ai). WR/PF live.
           2026-09-22 gate check: ALL GATES PASSING.
             Gate 1 (counterfactual): +1103.9 EUR / 90 trades
             Gate 2 (APPROVE > REJECT avg): APPROVE -2.19 vs REJECT -31.67 EUR (PASS)
  Stocks : Phase C (shadow only) — Copilot observes ibkr_paper but does NOT act.
           Stocks WR/PF table populates automatically when Phase D is enabled
           and trades close with real prices.
           Next gate check: 2026-09-29 (Monday). Phase D requires ≥30 quality
           trades + written go/no-go from user.

Usage:
    python ibkr_copilot_trades_dashboard.py           # refresh every 30s
    python ibkr_copilot_trades_dashboard.py --once    # print once and exit
    python ibkr_copilot_trades_dashboard.py --today   # today-only shadow section
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).parent
_LEDGER  = _ROOT / "data" / "pnl_ledger.db"
_IBKR_DB = _ROOT / "data" / "ibkr_stocks.db"
_SHADOW  = _ROOT / "data" / "ai_shadow_decisions.jsonl"

# ── ANSI ──────────────────────────────────────────────────────────────────────
_WIN = sys.platform == "win32"
if _WIN:
    import ctypes
    try:
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

R  = "\033[91m"; G  = "\033[92m"; Y  = "\033[93m"; C  = "\033[96m"
DIM = "\033[2m"; BOLD = "\033[1m"; RST = "\033[0m"

def _c(t, col): return f"{col}{t}{RST}"
def _pct(n, tot): return f"{100*n//tot:3d}%" if tot else "  —%"
def _pf(gp, gl): return f"{gp/gl:.2f}" if gl else "∞   "
def _wr_col(wr): return G if wr >= 50 else (Y if wr >= 40 else R)


# ── Data ──────────────────────────────────────────────────────────────────────

def _ledger_forex_ai():
    """Return closed forex_ai trades as list of dicts."""
    if not _LEDGER.exists():
        return []
    con = sqlite3.connect(_LEDGER)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT strategy, symbol, direction, realized_pnl, currency, "
        "timestamp_open, timestamp_close, exit_reason "
        "FROM trades WHERE module='forex_ai' AND status='closed' "
        "ORDER BY timestamp_close"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _ibkr_closed():
    """Return closed IBKR stocks trades (status=SOLD) with meaningful prices."""
    if not _IBKR_DB.exists():
        return []
    con = sqlite3.connect(_IBKR_DB)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT b.symbol, b.strategy, b.qty, b.fill_price as entry, b.filled_at, "
        "  (SELECT s.fill_price FROM trades s "
        "   WHERE s.symbol=b.symbol AND s.side='SELL' AND s.status='FILLED' "
        "   ORDER BY s.filled_at DESC LIMIT 1) as exit_p "
        "FROM trades b WHERE b.side='BUY' AND b.status='SOLD' "
        "  AND b.fill_price > 0"
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        e, x = float(r['entry'] or 0), float(r['exit_p'] or 0)
        if e > 0 and x > 0:
            pnl = (x - e) * float(r['qty'])
            out.append({'symbol': r['symbol'], 'strategy': r['strategy'],
                        'entry': e, 'exit': x, 'qty': float(r['qty']),
                        'pnl': pnl, 'date': str(r['filled_at'])[:10]})
    return out


def _shadow_decisions():
    """Load all shadow decisions from jsonl."""
    if not _SHADOW.exists():
        return []
    with open(_SHADOW, encoding='utf-8') as f:
        return [json.loads(l) for l in f if l.strip()]


def _stats(trades, pnl_key='realized_pnl'):
    cnt = len(trades)
    wins = sum(1 for t in trades if float(t.get(pnl_key, 0) or 0) > 0)
    gp = sum(float(t[pnl_key]) for t in trades if float(t.get(pnl_key, 0) or 0) > 0)
    gl = sum(abs(float(t[pnl_key])) for t in trades if float(t.get(pnl_key, 0) or 0) <= 0)
    net = sum(float(t.get(pnl_key, 0) or 0) for t in trades)
    wr = int(100 * wins / cnt) if cnt else 0
    return cnt, wins, wr, gp, gl, net


# ── Render ────────────────────────────────────────────────────────────────────

def _render_forex(forex_trades: list) -> None:
    cnt, wins, wr, gp, gl, net = _stats(forex_trades)
    pf_str = _pf(gp, gl)
    ccy = forex_trades[0]['currency'] if forex_trades else 'EUR'
    wr_col = _wr_col(wr)

    print(f"\n  {BOLD}Forex AI Copilot  (ai_sim book){RST}  {DIM}· {cnt} closed trades · source: pnl_ledger{RST}")
    print(f"  {'─'*60}")
    print(f"  Closed trades : {BOLD}{cnt}{RST}")
    print(f"  Win rate      : {_c(f'{wr}%', wr_col)}")
    try:
        pf_overall_col = G if float(pf_str.strip()) >= 1 else R
    except ValueError:
        pf_overall_col = G
    print(f"  Profit factor : {_c(pf_str, pf_overall_col)}")
    print(f"  Net P&L       : {_c(f'{net:+.2f} {ccy}', G if net >= 0 else R)}")
    print(f"  Gross profit  : {_c(f'+{gp:.2f}', G)}  |  Gross loss: {_c(f'-{gl:.2f}', R)}")

    print(f"\n  {'Strategy':<26}  {'Trades':>6}  {'WR':>5}  {'PF':>6}  {'Net P&L':>12}  Bar")
    print(f"  {'─'*26}  {'─'*6}  {'─'*5}  {'─'*6}  {'─'*12}  {'─'*20}")
    by_strat: dict[str, list] = defaultdict(list)
    for t in forex_trades:
        by_strat[t['strategy']].append(t)
    for strat in sorted(by_strat, key=lambda s: -len(by_strat[s])):
        sc, sw, swr, sgp, sgl, snet = _stats(by_strat[strat])
        spf = _pf(sgp, sgl)
        bar = '█' * max(1, int(swr / 5))
        col = _wr_col(swr)
        try:
            pf_col = G if float(spf.strip()) >= 1.0 else R
        except ValueError:
            pf_col = G
        print(f"  {strat:<26}  {sc:>6}  {_c(f'{swr}%', col):>5}  "
              f"{_c(spf, pf_col):>6}  "
              f"{_c(f'{snet:+.2f}', G if snet >= 0 else R):>12}  "
              f"{_c(bar, col)}")

    print(f"\n  {BOLD}Recent closed trades (last 15){RST}")
    print(f"  {'─'*75}")
    print(f"  {'Close':<10}  {'Symbol':<10}  {'Strategy':<22}  {'P&L':>10}  Exit reason")
    print(f"  {'─'*10}  {'─'*10}  {'─'*22}  {'─'*10}  {'─'*20}")
    for t in reversed(forex_trades[-15:]):
        pnl    = float(t['realized_pnl'])
        col    = G if pnl > 0 else R
        dt     = str(t['timestamp_close'])[:10]
        reason = str(t.get('exit_reason') or '')[:20]
        print(f"  {dt:<10}  {t['symbol']:<10}  {t['strategy']:<22}  "
              f"{_c(f'{pnl:+.2f}', col):>10}  {DIM}{reason}{RST}")


def _render_stocks(ibkr_trades: list, shadow: list, today: str) -> None:
    ibkr_shadow  = [d for d in shadow if d.get('account_env') == 'ibkr_paper']
    ibkr_applied = [d for d in ibkr_shadow if d.get('applied') is True]
    live_shadow  = [d for d in shadow if d.get('account_env') in ('live', 'live_eur')]

    print(f"\n  {BOLD}IBKR Stocks Copilot  (ibkr_paper book){RST}  {DIM}· source: ibkr_stocks.db + shadow log{RST}")
    print(f"  {'─'*70}")
    if ibkr_trades:
        cnt2, wins2, wr2, gp2, gl2, net2 = _stats(ibkr_trades, 'pnl')
        print(f"  Closed trades : {BOLD}{cnt2}{RST}")
        print(f"  Win rate      : {_c(f'{wr2}%', _wr_col(wr2))}")
        print(f"  Profit factor : {_pf(gp2, gl2)}")
        print(f"  Net P&L       : {_c(f'{net2:+.2f} USD', G if net2 >= 0 else R)}")
    else:
        print(f"  {Y}Phase C — shadow observation only.{RST}  Copilot decides but does NOT act on ibkr_paper.")
        print(f"  Trade performance will appear here once Phase D is enabled.")

    by_action: dict[str, int] = defaultdict(int)
    for d in ibkr_shadow:
        by_action[d.get('agent_action', '?')] += 1
    total_sh = sum(by_action.values())
    if total_sh:
        print(f"\n  Shadow decisions ({total_sh} total, {len(ibkr_applied)} applied):")
        for action, col in [('APPROVE', G), ('REJECT', R), ('MODIFY', Y), ('HOLD', DIM)]:
            n = by_action.get(action, 0)
            if n:
                bar = '█' * max(1, n * 30 // total_sh)
                print(f"    {_c(f'{action:<8}', col)}  {_c(bar, col)}  {n:>4}  {_pct(n, total_sh)}")

    today_ibkr = [d for d in ibkr_shadow if d.get('ts', '')[:10] == today]
    if today_ibkr:
        print(f"\n  Today's shadow decisions ({len(today_ibkr)}):")
        print(f"  {'Time':<8}  {'Action':<9}  {'Strategy':<20}  {'Symbol':<8}  {'Mult':>5}  Comment")
        print(f"  {'─'*8}  {'─'*9}  {'─'*20}  {'─'*8}  {'─'*5}  {'─'*35}")
        for d in sorted(today_ibkr, key=lambda x: x.get('ts', ''))[-20:]:
            ts    = d.get('ts', '')[-15:-9]
            act   = d.get('agent_action', '?')
            col   = {'APPROVE': G, 'REJECT': R, 'MODIFY': Y, 'HOLD': DIM}.get(act, '')
            strat = str(d.get('strategy', '?'))[:20]
            sym   = str(d.get('symbol', '?'))[:8]
            mult  = d.get('agent_size_multiplier')
            ms    = f"{mult:.2f}x" if mult and act == 'MODIFY' else "  —  "
            cmt   = str(d.get('agent_comment', ''))[:55]
            print(f"  {ts:<8}  {_c(f'{act:<9}', col)}  {strat:<20}  {sym:<8}  {ms:>5}  {DIM}{cmt}{RST}")

    if live_shadow:
        print(f"\n  {BOLD}Live Stocks Copilot  (Saxo real money){RST}  {DIM}· shadow observation{RST}")
        print(f"  {'─'*60}")
        by_a2: dict[str, int] = defaultdict(int)
        for d in live_shadow:
            by_a2[d.get('agent_action', '?')] += 1
        total2 = sum(by_a2.values())
        print(f"  Shadow decisions: {total2} total, "
              f"{sum(1 for d in live_shadow if d.get('applied') is True)} applied")
        for action, col in [('APPROVE', G), ('REJECT', R), ('MODIFY', Y)]:
            n = by_a2.get(action, 0)
            if n:
                bar = '█' * max(1, n * 20 // total2)
                print(f"    {_c(f'{action:<8}', col)}  {_c(bar, col)}  {n:>3}  {_pct(n, total2)}")


def _render(today_only: bool, show_forex: bool, show_stocks: bool) -> None:
    forex_trades = _ledger_forex_ai()
    ibkr_trades  = _ibkr_closed()
    shadow       = _shadow_decisions()
    today        = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_str      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    mode         = "Forex" if (show_forex and not show_stocks) else \
                   "Stocks" if (show_stocks and not show_forex) else "All"

    os.system("cls" if _WIN else "clear")
    print(f"\n  {BOLD}AI Copilot — Trade Performance{RST}  {DIM}· {mode} · {now_str}{RST}")
    print(f"  {'═'*90}")

    if show_forex:
        _render_forex(forex_trades)
    if show_stocks:
        _render_stocks(ibkr_trades, shadow, today)

    print(f"\n  {'─'*90}")
    print(f"  {DIM}Forex AI Copilot = ai_sim book (applied decisions) · Stocks = Phase C shadow only{RST}")
    print(f"  {DIM}Ctrl+C to quit  ·  --once to print once  ·  --forex / --stocks to filter{RST}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",     action="store_true")
    ap.add_argument("--today",    action="store_true")
    ap.add_argument("--forex",    action="store_true", help="Show Forex section only")
    ap.add_argument("--stocks",   action="store_true", help="Show Stocks section only")
    ap.add_argument("--interval", type=int, default=30)
    args = ap.parse_args()

    # If neither flag given, show both
    show_forex  = args.forex  or (not args.forex and not args.stocks)
    show_stocks = args.stocks or (not args.forex and not args.stocks)

    try:
        while True:
            _render(today_only=args.today, show_forex=show_forex, show_stocks=show_stocks)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n  Bye.\n")


if __name__ == "__main__":
    main()
