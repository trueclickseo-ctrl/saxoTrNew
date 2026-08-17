"""
pnl_dashboard.py  —  Unified P&L statement across all 4 modules
----------------------------------------------------------------
Usage:
    python pnl_dashboard.py            # live-refresh every 60s
    python pnl_dashboard.py --once     # print once and exit
    python pnl_dashboard.py --sync     # sync state files first, then display
    python pnl_dashboard.py --module stock|etf|futures|forex
"""

import os, sys, json, time
from datetime import datetime, date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import pnl_tracker

REFRESH_SECONDS = 60

# ── VT100 ─────────────────────────────────────────────────────────
def _enable_vt():
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h   = k32.GetStdHandle(-11)
        m   = ctypes.c_ulong()
        k32.GetConsoleMode(h, ctypes.byref(m))
        k32.SetConsoleMode(h, m.value | 0x4)
    except Exception:
        pass
    os.system("")

_enable_vt()

GR  = "\033[92m"; RD  = "\033[91m"; YL  = "\033[93m"
BL  = "\033[94m"; CY  = "\033[96m"; MG  = "\033[95m"
W   = "\033[0m";  BD  = "\033[1m";  DM  = "\033[2m"
HOME  = "\033[H"; CLEAR = "\033[2J"

MOD_COL = {"stock": BL, "etf": YL, "futures": MG, "forex": CY}
MOD_LABEL = {
    "stock":   "STOCKS",
    "etf":     "ETF",
    "futures": "FUTURES",
    "forex":   "FOREX",
}
MOD_DESC = {
    "stock":   "US Blend momentum  |  SEK",
    "etf":     "Sector rotation  SL 8% / TP 20%  |  USD",
    "futures": "Donchian · RSI · EMA  |  ES NQ GC CL ZB  |  USD",
    "forex":   "EMA · RSI(2) · Donchian  |  7 FX pairs  |  USD",
}


def _bar(value: float, total: float, width: int = 20, colour: str = GR) -> str:
    pct   = abs(value / total) if total else 0
    filled = min(int(pct * width), width)
    bar    = "█" * filled + "░" * (width - filled)
    return f"{colour}{bar}{W}"


def _sign_col(v: float) -> str:
    return GR if v >= 0 else RD


def _fmt_pnl(v: float, cur: str = "USD", width: int = 14) -> str:
    sign = "+" if v >= 0 else ""
    s    = f"{sign}{v:,.2f} {cur}"
    return f"{_sign_col(v)}{BD}{s:>{width}}{W}"


def _render(module_filter: str = None) -> str:
    now_ts  = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    summary = pnl_tracker.get_summary(module_filter)
    modules = [module_filter] if module_filter else list(pnl_tracker.MODULES)

    W_TOTAL = 104
    HR  = f"  {DM}{'─'*W_TOTAL}{W}"
    DBL = f"  {BD}{'═'*W_TOTAL}{W}"

    L = []

    # ── Master header ─────────────────────────────────────────────
    L.append(f"  {BD}{CY}╔{'═'*W_TOTAL}╗{W}")
    L.append(f"  {BD}{CY}║{'  QUANT BOT  —  P&L STATEMENT':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{CY}║{f'  Stocks · ETF · Futures · Forex  |  {now_ts}':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{CY}╚{'═'*W_TOTAL}╝{W}")
    L.append("")

    # ── Grand total banner ────────────────────────────────────────
    tot  = summary.get("total", {})
    tpnl = tot.get("realized_pnl", 0.0)
    # Recompute counts for USD-only modules (stock is SEK — keep separate)
    usd_mods   = ("etf", "futures", "forex")
    usd_closed = sum(summary.get(m, {}).get("closed_trades", 0) for m in usd_mods)
    usd_wins   = sum(summary.get(m, {}).get("wins", 0)          for m in usd_mods)
    usd_wr     = round(usd_wins / usd_closed * 100, 1) if usd_closed else 0.0
    tc   = _sign_col(tpnl)

    L.append(DBL)
    L.append(
        f"  {BD}REALIZED P&L (ETF+Futures+Forex USD){W}   "
        f"{tc}{BD}{tpnl:>+,.2f} USD{W}     "
        f"{DM}Closed: {usd_closed}  |  Win rate: {usd_wr:.1f}%{W}"
    )

    # Stock P&L in SEK
    sk  = summary.get("stock", {})
    spnl = sk.get("realized_pnl", 0.0)
    sc   = _sign_col(spnl)
    L.append(
        f"  {BD}REALIZED P&L STOCKS (SEK){W}              "
        f"{sc}{BD}{spnl:>+,.2f} SEK{W}     "
        f"{DM}Closed: {sk.get('closed_trades',0)}  |  Win rate: {sk.get('win_rate',0):.1f}%{W}"
    )
    L.append(DBL)
    L.append("")

    # ── Per-module breakdown ──────────────────────────────────────
    for mod in modules:
        s    = summary.get(mod, {})
        col  = MOD_COL.get(mod, DM)
        cur  = "SEK" if mod == "stock" else "USD"
        pnl  = s.get("realized_pnl", 0.0)
        n    = s.get("closed_trades", 0)
        op   = s.get("open_trades",   0)
        wr   = s.get("win_rate",      0.0)
        wins = s.get("wins",           0)
        loss = s.get("losses",         0)
        pf   = s.get("profit_factor")
        best = s.get("best_trade",     0.0)
        wrst = s.get("worst_trade",    0.0)
        avg  = s.get("avg_pnl",        0.0)
        pc   = _sign_col(pnl)

        L.append(f"  {col}{BD}{'━'*W_TOTAL}{W}")
        L.append(
            f"  {col}{BD}{MOD_LABEL.get(mod,mod.upper()):<10}{W}  "
            f"{DM}{MOD_DESC.get(mod,'')}{W}"
        )
        L.append(f"  {col}{BD}{'━'*W_TOTAL}{W}")

        # Stats grid
        pf_s  = f"{pf:.2f}" if pf else "—"
        wr_c  = GR if wr >= 50 else (YL if wr >= 40 else RD)
        L.append(
            f"  {'Closed trades':<18}: {BD}{n:>5}{W}    "
            f"{'Open positions':<18}: {BD}{op:>5}{W}    "
            f"{'Win rate':<12}: {wr_c}{BD}{wr:.1f}%{W}    "
            f"{'Profit factor':<14}: {BD}{pf_s}{W}"
        )
        L.append(
            f"  {'Wins':<18}: {GR}{BD}{wins:>5}{W}    "
            f"{'Losses':<18}: {RD}{BD}{loss:>5}{W}    "
            f"{'Avg per trade':<12}: {_sign_col(avg)}{BD}{avg:>+.2f}{W} {DM}{cur}{W}  "
            f"{'':14}"
        )
        L.append(
            f"  {'Best trade':<18}: {GR}{BD}+{best:>10,.2f}{W} {DM}{cur}{W}    "
            f"{'Worst trade':<18}: {RD}{BD}{wrst:>+10,.2f}{W} {DM}{cur}{W}"
        )

        # Realized P&L bar
        abs_max = max(abs(pnl), 1)
        bar = _bar(pnl, abs_max, width=30, colour=pc)
        L.append("")
        L.append(
            f"  {BD}REALIZED P&L{W}  "
            f"[{bar}]  "
            f"{pc}{BD}{pnl:>+,.2f} {cur}{W}"
        )

        # Recent closed trades (last 5)
        closed = pnl_tracker.get_closed_trades(mod, limit=5)
        if closed:
            L.append("")
            L.append(f"  {DM}Recent closed trades (last 5):{W}")
            L.append(
                f"  {DM}  {'Date':<12}{'Symbol':<9}{'Strategy':<14}"
                f"{'Direction':<8}{'Qty':>12}  {'Entry':>10}  {'Exit':>10}  "
                f"{'P&L':>12}  Exit reason{W}"
            )
            for t in closed:
                rpnl = t.get("realized_pnl") or 0
                rc   = _sign_col(rpnl)
                ep   = t.get("entry_price") or 0
                xp   = t.get("exit_price")  or 0
                dp   = (t.get("timestamp_close") or "")[:10]
                direc  = (t.get("direction") or "").upper()
                side_s = "Buy" if direc == "BUY" else "Sell"
                L.append(
                    f"    {DM}{dp:<12}{W}{BD}{(t.get('symbol') or ''):<9}{W}"
                    f"{DM}{(t.get('strategy') or ''):<14}{W}"
                    f"{side_s:<8}"
                    f"{DM}{(t.get('quantity') or 0):>12,.0f}{W}  "
                    f"{ep:>10.4f}  {xp:>10.4f}  "
                    f"{rc}{BD}{rpnl:>+,.2f}{W}  "
                    f"{DM}{(t.get('exit_reason') or '—')[:28]}{W}"
                )

        L.append("")

    # ── Open positions summary ────────────────────────────────────
    L.append(HR)
    L.append(f"  {BD}OPEN POSITIONS (all modules){W}")
    L.append("")
    open_all = pnl_tracker.get_open_positions(module_filter)
    if open_all:
        L.append(
            f"  {DM}  {'Module':<10}{'Strategy':<14}{'Symbol':<9}"
            f"{'Direction':<8}{'Qty':>12}  {'Entry':>10}  {'Stop':>10}  "
            f"{'Opened':<12}{W}"
        )
        L.append(HR)
        for t in open_all:
            col   = MOD_COL.get(t.get("module"), DM)
            direc = (t.get("direction") or "").upper()
            side_s = "Buy" if direc in ("BUY", "Buy") else "Sell"
            is_long = side_s == "Buy"
            side_c  = GR if is_long else RD
            L.append(
                f"  {col}{(t.get('module') or ''):<10}{W}"
                f"{DM}{(t.get('strategy') or ''):<14}{W}"
                f"{BD}{(t.get('symbol') or ''):<9}{W}"
                f"{side_c}{side_s:<8}{W}"
                f"{DM}{(t.get('quantity') or 0):>12,.0f}{W}  "
                f"{(t.get('entry_price') or 0):>10.5f}  "
                f"{(t.get('stop_price') or 0):>10.5f}  "
                f"{DM}{(t.get('timestamp_open') or '')[:10]}{W}"
            )
    else:
        L.append(f"  {DM}No open positions in ledger — run: python pnl_tracker.py --sync{W}")

    L.append(HR)
    L.append("")
    L.append(
        f"  {DM}DB: {pnl_tracker.DB_PATH}  |  "
        f"Sync: python pnl_tracker.py --sync  |  "
        f"Statement: python pnl_tracker.py --statement  |  "
        f"Refreshes every {REFRESH_SECONDS}s{W}"
    )
    L.append("")
    return "\n".join(L)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",   action="store_true")
    ap.add_argument("--sync",   action="store_true", help="Sync state files before display")
    ap.add_argument("--module", choices=list(pnl_tracker.MODULES))
    args = ap.parse_args()

    if args.sync:
        counts = pnl_tracker.sync_all()
        print(f"Synced: {counts}")
        time.sleep(1)

    if args.once:
        sys.stdout.write(_render(args.module))
        sys.stdout.flush()
        return

    while True:
        try:
            out = _render(args.module)
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            sys.stdout.write(out)
            sys.stdout.flush()
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            sys.stdout.write(f"\n{W}")
            break


if __name__ == "__main__":
    main()
