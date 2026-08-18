"""
etf_dashboard.py  —  Live ETF Rotation dashboard
-------------------------------------------------
Usage:
    python etf_dashboard.py            # refresh every 60s
    python etf_dashboard.py --fast     # refresh every 10s
    python etf_dashboard.py --once     # print once and exit
"""

import os, sys, json, time
from datetime import datetime, date

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
ETF_STATE     = os.path.join(BASE_DIR, "saxo_etf_strategy", "data", "etf_positions.json")
ETF_LOG       = os.path.join(BASE_DIR, "saxo_etf_strategy", "data", "etf_runner.log")

sys.path.insert(0, BASE_DIR)
import price_service

REFRESH_SECONDS = 60
STOP_LOSS_PCT   = 0.08   # matches etf_config.py ETFRiskConfig.stop_loss_pct
TAKE_PROFIT_PCT = 0.20   # matches etf_config.py ETFRiskConfig.take_profit_pct

# ── Windows console clear ──────────────────────────────────────────
def _clear_console():
    try:
        import ctypes, struct
        k32  = ctypes.windll.kernel32
        h    = k32.GetStdHandle(-11)
        buf  = ctypes.create_string_buffer(22)
        k32.GetConsoleScreenBufferInfo(h, buf)
        _, _, _, _, _, left, top, right, bottom, _, _ = struct.unpack("hhhhHhhhhhh", buf.raw)
        cols = right - left + 1; rows = bottom - top + 1; size = cols * rows
        done = ctypes.c_ulong(0)
        k32.FillConsoleOutputCharacterW(h, 32, size, 0, ctypes.byref(done))
        k32.FillConsoleOutputAttribute(h, 7,  size, 0, ctypes.byref(done))
        k32.SetConsoleCursorPosition(h, 0)
    except Exception:
        sys.stdout.write("\033[2J\033[H"); sys.stdout.flush()

try:
    import ctypes as _ct
    _k32 = _ct.windll.kernel32; _h = _k32.GetStdHandle(-11); _m = _ct.c_ulong()
    _k32.GetConsoleMode(_h, _ct.byref(_m)); _k32.SetConsoleMode(_h, _m.value | 0x4)
except Exception:
    pass

# ── Colours ───────────────────────────────────────────────────────
GR = "\033[92m"; RD = "\033[91m"; YL = "\033[93m"
BL = "\033[94m"; CY = "\033[96m"; MG = "\033[95m"
W  = "\033[0m";  BD = "\033[1m";  DM = "\033[2m"
WH = "\033[97m"

import re as _re
def _len(s):  return len(_re.sub(r'\033\[[0-9;]*m', '', s))
def _rpad(s, w): return s + ' ' * max(0, w - _len(s))

def _c(n, fmt=".2f"):  return f"{n:{fmt}}" if n is not None else "—"
def _pnl(n, cur="USD"):
    if n is None: return f"{DM}—{W}"
    c = GR if n >= 0 else RD; sgn = "+" if n >= 0 else ""
    return f"{c}{BD}{sgn}{n:,.2f} {cur}{W}"
def _pct(n):
    if n is None: return f"{DM}—{W}"
    c = GR if n >= 0 else RD; sgn = "+" if n >= 0 else ""
    return f"{c}{sgn}{n:.2f}%{W}"


# ── Data readers ──────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.load(open(ETF_STATE, encoding="utf-8"))
    except Exception:
        return {"positions": {}, "orders": []}

def _last_log_lines(n: int = 8) -> list:
    for path in [ETF_LOG,
                 os.path.join(BASE_DIR, "logs", "etf.log"),
                 os.path.join(BASE_DIR, "data", "etf_runner.log")]:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    lines = [l.rstrip() for l in f.readlines() if l.strip()]
                return lines[-n:]
            except Exception:
                pass
    return []

def _days_held(ts: str) -> int:
    try:
        return (date.today() - date.fromisoformat(ts[:10])).days
    except Exception:
        return 0


# ── Render ────────────────────────────────────────────────────────
def _render(once: bool = False, interval: int = REFRESH_SECONDS) -> str:
    state     = _load_state()
    positions = state.get("positions", {})    # {uic_str: {symbol, qty, entry_price, ...}}
    orders    = state.get("orders", [])

    now_ts = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    token  = price_service.load_token()

    # ETF infoprices return NoAccess on SIM — use positions API CurrentPrice instead.
    # On a live account both work; positions API is more reliable regardless.
    live_px   = {}
    price_src = "unavailable"
    if token:
        try:
            import requests
            r = requests.get(
                "https://gateway.saxobank.com/sim/openapi/port/v1/positions/me",
                headers={"Authorization": f"Bearer {token}"},
                params={"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"},
                timeout=10,
            )
            if r.status_code == 200:
                _ETYPES = {"Etf", "CfdOnEtf", "CdfOnEtf"}
                for p in r.json().get("Data", []):
                    pb  = p.get("PositionBase", {})
                    pv  = p.get("PositionView", {})
                    dis = p.get("DisplayAndFormat", {})
                    if pb.get("AssetType") not in _ETYPES:
                        continue
                    sym = dis.get("Symbol", "")
                    px  = float(pv.get("CurrentPrice") or 0)
                    ep  = float(pb.get("OpenPrice") or 0)
                    qty = float(pb.get("Amount") or 0)
                    pnl = float(pv.get("ProfitLossOnTrade") or 0)
                    # Back-calc current price from P&L when CurrentPrice is missing
                    if px == 0 and ep > 0 and qty != 0:
                        px = ep + pnl / qty
                    if sym and px > 0:
                        live_px[sym.split(":")[0]] = round(px, 2)
                if live_px:
                    price_src = "saxo"
        except Exception:
            pass

    W_TOTAL = 102
    HR      = f"  {DM}{'─' * W_TOTAL}{W}"
    L       = []

    # ── Header ───────────────────────────────────────────────────
    # Saxo SIM returns NoAccess for ETF/Stock infoprices — live prices only work on a live account
    has_live = bool(live_px)
    if not token:
        src_tag = f"{RD}token expired — run: python set_token.py{W}"
    elif not has_live:
        src_tag = f"{YL}SIM account — ETF prices not available (live account required){W}"
    else:
        src_tag = f"{GR}SAXO LIVE{W}"
    L.append(f"  {BD}{YL}╔{'═' * W_TOTAL}╗{W}")
    L.append(f"  {BD}{YL}║{'  ETF ROTATION DASHBOARD':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{YL}║{f'  Sector Momentum · EMA Trend Score · Max 5 Positions  |  {now_ts}':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{YL}╚{'═' * W_TOTAL}╝{W}")
    L.append(f"  Prices: {src_tag}")
    L.append("")

    # ── Strategy info ─────────────────────────────────────────────
    L.append(
        f"  {BD}STRATEGY{W}   {YL}{BD}ETF Rotation{W}  "
        f"{DM}Buys top-scoring US sector ETFs using EMA(20/50/200) trend scoring  |  "
        f"Stop {STOP_LOSS_PCT*100:.0f}%  |  TP {TAKE_PROFIT_PCT*100:.0f}%  |  Max 5 slots{W}"
    )
    L.append(HR)
    L.append("")

    # ── Open positions table ──────────────────────────────────────
    L.append(f"  {BD}OPEN POSITIONS{W}  {DM}({len(positions)} active){W}")
    L.append("")
    L.append(
        f"  {DM}{'Symbol':<7}  {'UIC':>7}  {'Qty':>6}  {'Entry':>8}  "
        f"{'Now':>8}  {'Stop':>8}  {'TP':>8}  "
        f"{'P&L (USD)':>14}  {'%':>8}  {'Score':>7}  Days{W}"
    )
    L.append(HR)

    total_cost = 0.0
    total_now  = 0.0
    total_pnl  = 0.0
    near_stops = []
    any_live   = False

    if positions:
        for uic_str, pos in sorted(positions.items(), key=lambda x: x[1].get("symbol", "")):
            sym   = pos.get("symbol", "?")
            qty   = int(pos.get("quantity", 0))
            ep    = float(pos.get("entry_price", 0))
            score = float(pos.get("entry_score", 0))
            days  = _days_held(pos.get("entry_date", ""))
            stop  = round(ep * (1 - STOP_LOSS_PCT), 2)
            tp    = round(ep * (1 + TAKE_PROFIT_PCT), 2)

            now  = live_px.get(sym)
            pnl  = (now - ep) * qty if now else None
            pct  = ((now - ep) / ep * 100) if now and ep else None
            total_cost += ep * qty
            if now:
                any_live   = True
                total_now += now * qty
                total_pnl += (now - ep) * qty

            pc      = (GR if pnl >= 0 else RD) if pnl is not None else DM
            near    = now is not None and now < stop * 1.02
            stp_col = f"{RD}{BD}" if near else DM
            if near:
                near_stops.append(f"{sym}  now={now:.2f}  stop={stop:.2f}")

            now_s = f"{now:.2f}" if now else "—"
            pnl_s = f"{'+'if pnl>=0 else ''}{pnl:,.2f}" if pnl is not None else "—"
            pct_s = f"{'+'if pct>=0 else ''}{pct:.2f}%" if pct is not None else "—"

            L.append(
                f"  {BD}{sym:<7}{W}  {DM}{uic_str:>7}{W}  {qty:>6,}  "
                f"{DM}{ep:>8.2f}{W}  "
                f"{BD}{now_s:>8}{W}  "
                f"{stp_col}{stop:>8.2f}{W}  "
                f"{DM}{tp:>8.2f}{W}  "
                f"{_rpad(f'{pc}{pnl_s}{W}', 19)}  "
                f"{_rpad(f'{pc}{pct_s}{W}', 13)}  "
                f"{DM}{score:>7.4f}{W}  {DM}{days}d{W}"
            )

        L.append(HR)
        if any_live:
            t_pnl_col  = GR if total_pnl >= 0 else RD
            t_pnl_sign = "+" if total_pnl >= 0 else ""
            t_pct      = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0
            t_pct_sign = "+" if t_pct >= 0 else ""
            L.append(
                f"  {BD}TOTAL{W}  "
                f"{DM}{len(positions)} positions  |  Cost: ${total_cost:>,.0f}  |  "
                f"Now: ${total_now:>,.0f}  |  Unrealized P&L: {W}"
                f"{t_pnl_col}{BD}{t_pnl_sign}{total_pnl:,.2f} USD  ({t_pct_sign}{t_pct:.2f}%){W}"
            )
        else:
            L.append(
                f"  {BD}TOTAL{W}  "
                f"{DM}{len(positions)} positions  |  Cost: ${total_cost:>,.0f}  |  "
                f"Live prices unavailable on Saxo SIM{W}"
            )
        if near_stops:
            L.append(f"  {RD}{BD}⚠  {len(near_stops)} position(s) within 2% of stop — review!{W}")
            for ns in near_stops:
                L.append(f"  {RD}   • {ns}{W}")
    else:
        L.append(f"  {DM}No open ETF positions.{W}")

    L.append(HR)
    L.append("")

    # ── Order history ─────────────────────────────────────────────
    recent_orders = [o for o in reversed(orders)][:20]
    L.append(f"  {BD}ORDER HISTORY{W}  {DM}(last 20){W}")
    L.append("")
    L.append(
        f"  {DM}{'Date':<10}  {'Side':<4}  {'Symbol':<7}  "
        f"{'Qty':>6}  {'Price':>8}  {'P&L (USD)':>12}  {'%':>8}  Notes{W}"
    )
    L.append(HR)

    if recent_orders:
        # Build a set of currently open symbols for open-position highlighting
        open_syms = {pos.get("symbol") for pos in positions.values()}
        for o in recent_orders:
            side  = (o.get("side") or "").upper()
            sym   = o.get("symbol", "")
            qty   = int(o.get("quantity", 0))
            ep    = float(o.get("entry_price") or 0)
            xp    = float(o.get("exit_price")  or 0)
            dry   = o.get("dry_run", False)
            ts    = (o.get("timestamp") or "")[:10]

            if dry:
                side_s = f"{DM}BUY*{W}"
            elif side == "BUY":
                side_s = f"{GR}{BD}BUY {W}"
            else:
                side_s = f"{CY}SELL{W}"

            pnl = None; pct = None; note = ""
            if side == "SELL" and ep > 0 and xp > 0:
                pnl = (xp - ep) * qty
                pct = (xp - ep) / ep * 100
                note = "closed"
            elif side == "BUY" and sym in open_syms:
                now = live_px.get(sym)
                if now and ep > 0:
                    pnl = (now - ep) * qty
                    pct = (now - ep) / ep * 100
                note = f"{DM}open{W}"

            pc    = (GR if pnl >= 0 else RD) if pnl is not None else DM
            pnl_s = f"{'+'if pnl>=0 else ''}{pnl:,.2f}" if pnl is not None else "—"
            pct_s = f"{'+'if pct>=0 else ''}{pct:.2f}%" if pct is not None else "—"
            price = ep if side == "BUY" else xp

            L.append(
                f"  {ts:<10}  {side_s}  {BD}{sym:<7}{W}  "
                f"{qty:>6,}  {DM}{price:>8.2f}{W}  "
                f"{_rpad(f'{pc}{pnl_s}{W}', 17):}  "
                f"{_rpad(f'{pc}{pct_s}{W}', 13):}  {note}"
            )
    else:
        L.append(f"  {DM}No orders yet.{W}")

    L.append(HR)
    L.append("")

    # ── P&L Ledger ────────────────────────────────────────────────
    try:
        import pnl_tracker
        summary = pnl_tracker.get_summary("etf")
        s       = summary.get("etf", {})
        pnl_r   = s.get("realized_pnl", 0.0)
        n_cl    = s.get("closed_trades", 0)
        wr      = s.get("win_rate", 0.0)
        best    = s.get("best_trade", 0.0)
        worst   = s.get("worst_trade", 0.0)
        pf      = s.get("profit_factor") or "—"
        pc      = GR if pnl_r >= 0 else RD
        L.append(f"  {BD}P&L LEDGER{W}  {DM}(realized — run pnl_dashboard.py for full view){W}")
        L.append(HR)
        L.append(
            f"  {BD}Realized P&L:{W}  {pc}{BD}{'+'if pnl_r>=0 else ''}{pnl_r:,.2f} USD{W}     "
            f"{DM}Closed: {n_cl}  |  WR: {wr:.1f}%  |  "
            f"Best: +{best:.2f}  |  Worst: {worst:+.2f}  |  PF: {pf}{W}"
        )
        L.append(HR)
    except Exception:
        pass

    # ── Recent log ────────────────────────────────────────────────
    log_lines = _last_log_lines(8)
    if log_lines:
        L.append("")
        L.append(f"  {BD}RUNNER LOG{W}  {DM}(last 8 lines){W}")
        L.append("")
        for line in log_lines:
            if "ERROR" in line or "error" in line:
                L.append(f"  {RD}{line}{W}")
            elif "BUY" in line or "placed" in line:
                L.append(f"  {GR}{line}{W}")
            elif "SELL" in line or "exit" in line.lower():
                L.append(f"  {YL}{line}{W}")
            else:
                L.append(f"  {DM}{line}{W}")
        L.append(HR)

    # ── Footer ───────────────────────────────────────────────────
    if not once:
        L.append(
            f"  {DM}Refreshes every {interval}s  |  Ctrl+C to exit  |  "
            f"Run: python saxo_etf_strategy/run_etf.py  to force scan{W}"
        )
    L.append("")
    return "\n".join(L)


def main():
    fast     = "--fast" in sys.argv
    once     = "--once" in sys.argv
    interval = 10 if fast else REFRESH_SECONDS

    if once:
        sys.stdout.write(_render(once=True, interval=interval))
        sys.stdout.flush()
        return

    while True:
        try:
            out = _render(interval=interval)
            _clear_console()
            sys.stdout.write(out)
            sys.stdout.flush()
            time.sleep(interval)
        except KeyboardInterrupt:
            sys.stdout.write(f"\n{W}")
            break


if __name__ == "__main__":
    main()
