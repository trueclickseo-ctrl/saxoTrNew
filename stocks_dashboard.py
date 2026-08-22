"""
stocks_dashboard.py  —  Live US Stocks (ATOS) dashboard
--------------------------------------------------------
Usage:
    python stocks_dashboard.py            # refresh every 30s
    python stocks_dashboard.py --fast     # refresh every 5s
    python stocks_dashboard.py --once     # print once and exit
"""

import os, sys, json, time, sqlite3
from datetime import datetime, date, timezone, timedelta

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(BASE_DIR, "data", "atos_live.db")
TRADE_LOG_PATH  = os.path.join(BASE_DIR, "data", "trade_log.csv")
ATOS_STATUS     = os.path.join(BASE_DIR, "data", "atos_status.json")

sys.path.insert(0, BASE_DIR)
import price_service

REFRESH_SECONDS = 30
TRAILING_PCT    = 0.12
HARD_STOP_PCT   = 0.15

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

REGIME_COL = {"BULL": GR, "BEAR": RD, "SIDEWAYS": YL, "TRANSITION": BL, "MOMENTUM": CY}

import re as _re
def _len(s):      return len(_re.sub(r'\033\[[0-9;]*m', '', s))
def _rpad(s, w):  return s + ' ' * max(0, w - _len(s))

def _pnl(n, cur="SEK"):
    if n is None: return f"{DM}—{W}"
    c = GR if n >= 0 else RD; sgn = "+" if n >= 0 else ""
    return f"{c}{BD}{sgn}{n:,.0f} {cur}{W}"

def _pct(n):
    if n is None: return f"{DM}—{W}"
    c = GR if n >= 0 else RD; sgn = "+" if n >= 0 else ""
    return f"{c}{sgn}{n:.2f}%{W}"

def _reg(r):
    if not r or r == "—": return f"{DM}—{W}"
    return f"{REGIME_COL.get(r.upper(), DM)}{r}{W}"


# ── Helpers ───────────────────────────────────────────────────────
def _effective_stop(drow: dict) -> float:
    entry      = drow.get("entry_price", 0) or 0
    stop_price = drow.get("stop_price",  0) or 0
    trail_high = drow.get("trailing_stop_high") or entry
    if stop_price > 0:
        return stop_price
    if trail_high > 0:
        return max(trail_high * (1 - TRAILING_PCT), entry * (1 - HARD_STOP_PCT))
    return entry * (1 - HARD_STOP_PCT)

def _days_held(s: str) -> int:
    try:
        return (date.today() - date.fromisoformat(s[:10])).days
    except Exception:
        return 0

def _exit_info(drow: dict) -> str:
    strategy   = (drow.get("strategy") or "").strip()
    days       = _days_held(drow.get("entry_date", ""))
    if "Reversion" in strategy:
        try:
            sys.path.insert(0, BASE_DIR)
            from atos.us_reversion import MAX_HOLD_DAYS
        except Exception:
            MAX_HOLD_DAYS = 10
        left = max(MAX_HOLD_DAYS - days, 0)
        return f"Day {days}/{MAX_HOLD_DAYS} ({left}d left)"
    return f"Day {days}  (monthly reb)"

def _et_now():
    utc = datetime.now(timezone.utc); year = utc.year
    mar = datetime(year, 3, 8, 2, tzinfo=timezone.utc)
    while mar.weekday() != 6: mar += timedelta(days=1)
    nov = datetime(year, 11, 1, 2, tzinfo=timezone.utc)
    while nov.weekday() != 6: nov += timedelta(days=1)
    tz = timezone(timedelta(hours=-4 if mar <= utc < nov else -5))
    return utc.astimezone(tz)

def _mkt_open():
    et = _et_now(); wk = et.weekday()
    return wk < 5 and (9, 30) <= (et.hour, et.minute) < (16, 0)

def _usd_sek() -> float:
    # Saxo's own live quote (2026-08-22, explicit user direction: never
    # Yahoo for anything live/on a dashboard). 10.95 is a last-resort
    # placeholder only if Saxo has no live USDSEK quote at all right now.
    try:
        import saxo_fx
        rate = saxo_fx.rate_to_sek(["USD"]).get("USD")
        return rate if rate else 10.95
    except Exception:
        return 10.95


# ── Data readers ──────────────────────────────────────────────────
def _atos_status() -> dict:
    try:
        return json.load(open(ATOS_STATUS)) if os.path.exists(ATOS_STATUS) else {"status": "idle"}
    except Exception:
        return {"status": "idle"}

def _db_open() -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM trades WHERE exit_date IS NULL").fetchall()
        conn.close()
        out = {}
        for r in rows:
            base = (r["ticker"] or "").split(".")[0].split(":")[0].upper()
            out[base] = dict(r)
        return out
    except Exception:
        return {}

def _db_stats() -> dict:
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT
                COUNT(*) FILTER (WHERE exit_date IS NOT NULL)               AS closed,
                SUM(pnl_sek) FILTER (WHERE exit_date IS NOT NULL)           AS realized,
                COUNT(*) FILTER (WHERE exit_date IS NOT NULL AND pnl_sek>0) AS wins
            FROM trades""").fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception:
        return {}

def _read_trade_log(n: int = 15) -> list:
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    try:
        import csv
        with open(TRADE_LOG_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        out = []
        for r in rows[-n:]:
            pnl_raw = (r.get("pnl_sek") or "").strip()
            out.append({
                "date":     r.get("date", "")[:10],
                "action":   r.get("action", "").upper(),
                "ticker":   r.get("ticker", ""),
                "strategy": r.get("strategy", ""),
                "shares":   int(float(r.get("shares") or 0)),
                "price":    float(r.get("price_usd") or 0),
                "value":    float(r.get("value_sek") or 0),
                "pnl_sek":  float(pnl_raw) if pnl_raw else None,
            })
        return list(reversed(out))
    except Exception:
        return []

_STOCK_ASSET_TYPES = {"Stock", "CfdOnStock"}

try:
    from atos.universe import US_TICKERS as _US_TICKERS
    _ATOS_UNIVERSE = {t.upper() for t in _US_TICKERS}
except Exception:
    _ATOS_UNIVERSE = None

def _saxo_positions(token: str) -> list:
    """Fetch open positions from Saxo, filtered to ATOS-universe stocks only (excludes ETFs)."""
    if not token:
        return []
    try:
        import requests
        r = requests.get(
            "https://gateway.saxobank.com/sim/openapi/port/v1/positions/me",
            headers={"Authorization": f"Bearer {token}"},
            params={"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"},
            timeout=10,
        )
        if r.status_code == 200:
            all_pos = r.json().get("Data", [])
            out = []
            for p in all_pos:
                if p.get("PositionBase", {}).get("AssetType") not in _STOCK_ASSET_TYPES:
                    continue
                sym = p.get("DisplayAndFormat", {}).get("Symbol", "")
                base = sym.split(":")[0].upper()
                if _ATOS_UNIVERSE is not None and base not in _ATOS_UNIVERSE:
                    continue
                out.append(p)
            return out
    except Exception:
        pass
    return []


# ── Render ────────────────────────────────────────────────────────
def _render(once: bool = False, interval: int = REFRESH_SECONDS) -> str:
    token  = price_service.load_token()
    db     = _db_open()
    stats  = _db_stats()
    st     = _atos_status()
    trades = _read_trade_log(15)
    saxo   = _saxo_positions(token)

    et    = _et_now()
    mkt   = f"{GR}● OPEN{W}" if _mkt_open() else f"{RD}● CLOSED{W}"
    now_s = et.strftime("%Y-%m-%d  %H:%M:%S ET")

    W_TOTAL = 104
    HR      = f"  {DM}{'─' * W_TOTAL}{W}"
    L       = []

    # ── Header ───────────────────────────────────────────────────
    s = st.get("status", "idle")
    if s == "running":
        atos_line = f"  {GR}[RUNNING]  ATOS scanning the universe...{W}"
    elif s == "complete":
        ts_str = (st.get("timestamp") or "")[:16].replace("T", " ")
        atos_line = (
            f"  {BL}[DONE]{W}  Last scan {ts_str}   "
            f"{GR}{st.get('buy_count', 0)} BUY{W}  "
            f"{RD}{st.get('exit_count', 0)} EXIT{W}  "
            f"{DM}{st.get('blocked_count', 0)} blocked{W}"
        )
    else:
        atos_line = f"  {DM}[IDLE]  Next scan: 02:00 PKT{W}"

    mkt_plain = "● OPEN" if _mkt_open() else "● CLOSED"
    sub = f"  US Blend · US Reversion · Regime-Filtered  |  NYSE/NASDAQ  |  {now_s}  |  {mkt_plain}"
    L.append(f"  {BD}{BL}╔{'═' * W_TOTAL}╗{W}")
    L.append(f"  {BD}{BL}║{'  US STOCKS DASHBOARD  (ATOS)':^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{BL}║{sub:^{W_TOTAL}}║{W}")
    L.append(f"  {BD}{BL}╚{'═' * W_TOTAL}╝{W}")
    L.append(f"  {atos_line}   {mkt}")
    L.append("")

    # ── Strategy summary ──────────────────────────────────────────
    L.append(
        f"  {BD}STRATEGIES{W}   "
        f"{BL}{BD}■ US Blend{W}  {DM}Monthly momentum rebalance, SPY+QQQ+IWM regime filter{W}   "
        f"{CY}{BD}■ US Reversion{W}  {DM}RSI(2)<5 mean-reversion, max 10 trading days{W}"
    )
    L.append(HR)
    L.append("")

    # ── Open positions ────────────────────────────────────────────
    rows      = []
    total_pnl = 0.0
    near_stop = []

    if saxo:
        # Live from Saxo API
        for p in saxo:
            disp = p.get("DisplayAndFormat", {})
            pb   = p.get("PositionBase", {})
            pv   = p.get("PositionView", {})
            sym  = disp.get("Symbol", "?")
            base = sym.split(":")[0].upper()
            shs  = int(pb.get("Amount", 0) or 0)
            ep   = float(pb.get("OpenPrice", 0) or 0)
            pnl  = float(pv.get("ProfitLossOnTrade", 0) or 0)
            now  = float(pv.get("CurrentPrice", 0) or 0)
            if now == 0 and shs > 0 and ep > 0:
                now = ep + pnl / shs
            # Compute % from prices — Saxo SIM returns 0 for this field on stocks
            ppc = ((now - ep) / ep * 100) if now and ep else 0.0
            drow = db.get(base, {})
            stop = _effective_stop(drow)
            rows.append({
                "ticker": base, "shs": shs, "entry": ep, "now": now,
                "stop": stop, "pnl": pnl, "ppc": ppc,
                "regime": (drow.get("regime_at_entry") or "—")[:10],
                "strategy": (drow.get("strategy") or "—")[:12],
                "exit": _exit_info(drow), "live": True,
            })
            total_pnl += pnl
            if stop > 0 and now > 0 and now < stop * 1.05:
                near_stop.append(f"{base}  now={now:.2f}  stop={stop:.2f}")
    elif db:
        for base, drow in db.items():
            ep   = float(drow.get("entry_price", 0) or 0)
            shs  = int(drow.get("shares", 0) or 0)
            stop = _effective_stop(drow)
            rows.append({
                "ticker": base, "shs": shs, "entry": ep, "now": None,
                "stop": stop, "pnl": None, "ppc": None,
                "regime": (drow.get("regime_at_entry") or "—")[:10],
                "strategy": (drow.get("strategy") or "—")[:12],
                "exit": _exit_info(drow), "live": False,
            })

    if saxo:
        src_note = f"{GR}live from Saxo{W}"
    elif not token:
        src_note = f"{RD}token expired — run: python set_token.py{W}"
    else:
        # Token valid but no stock positions on Saxo (SIM returns NoAccess for stocks)
        src_note = f"{YL}Saxo SIM — stock P&L unavailable (live account required){W}"
    L.append(f"  {BD}OPEN POSITIONS{W}  {DM}({len(rows)} active){W}  {src_note}")
    L.append("")
    L.append(
        f"  {DM}{'Ticker':<7}  {'Shrs':>5}  {'Entry':>8}  {'Now':>8}  "
        f"{'Stop':>8}  {'P&L (USD)':>12}  {'Chg%':>7}  "
        f"{'Strategy':<14}  {'Exit Trigger':<22}  Regime{W}"
    )
    L.append(HR)

    if rows:
        for r in rows:
            ep    = r["entry"]; now = r["now"]; stop = r["stop"]
            pnl   = r["pnl"];   ppc = r["ppc"]
            live  = r["live"]
            near  = stop > 0 and now is not None and now < stop * 1.05

            pc      = (GR if pnl >= 0 else RD) if live and pnl is not None else DM
            stp_col = f"{RD}{BD}" if near else DM
            ex_col  = YL if "0d left" in r["exit"] or "1d left" in r["exit"] else DM
            reg_col = REGIME_COL.get(r["regime"].upper(), DM)

            now_s  = f"{now:.2f}"  if now  is not None and now  > 0 else "—"
            stop_s = f"{stop:.2f}" if stop > 0 else "—"
            pnl_s  = f"{'+'if pnl>=0 else ''}{pnl:,.0f}" if live and pnl is not None else "—"
            ppc_s  = f"{'+'if ppc>=0 else ''}{ppc:.2f}%" if live and ppc is not None else "—"

            L.append(
                f"  {BD}{r['ticker']:<7}{W}  {r['shs']:>5,}  "
                f"{DM}{ep:>8.2f}{W}  {BD}{now_s:>8}{W}  "
                f"{stp_col}{stop_s:>8}{W}  "
                f"{_rpad(f'{pc}{pnl_s}{W}', 17):}  "
                f"{_rpad(f'{pc}{ppc_s}{W}', 12):}  "
                f"{DM}{r['strategy']:<14}{W}  "
                f"{ex_col}{r['exit']:<22}{W}  "
                f"{reg_col}{r['regime']}{W}"
            )

        L.append(HR)
        any_live_pnl = any(r["live"] for r in rows)
        if any_live_pnl:
            tc = GR if total_pnl >= 0 else RD
            ts = "+" if total_pnl >= 0 else ""
            L.append(
                f"  {BD}{'TOTAL':<7}{W}  {len(rows):>5} pos"
                f"{'':>38}"
                f"{tc}{ts}{total_pnl:,.0f} USD{W}"
            )
        else:
            L.append(
                f"  {BD}{'TOTAL':<7}{W}  {len(rows):>5} pos"
                f"{'':>38}"
                f"{DM}Live prices unavailable on Saxo SIM{W}"
            )
        if near_stop:
            L.append(f"  {RD}{BD}⚠  {len(near_stop)} position(s) within 5% of stop — review!{W}")
            for ns in near_stop:
                L.append(f"  {RD}   • {ns}{W}")
    else:
        L.append(f"  {DM}No open stock positions.{W}")

    L.append(HR)
    L.append("")

    # ── Today's signals ───────────────────────────────────────────
    scan_actions = st.get("actions") or []
    if scan_actions:
        scan_ts = (st.get("timestamp") or "")[:16].replace("T", " ")
        buys    = [a for a in scan_actions if a.get("action") == "BUY"]
        exits   = [a for a in scan_actions if a.get("action") in ("EXIT", "EXIT(FAILED)")]
        blocked = [a for a in scan_actions if a.get("action") == "BLOCKED"]

        L.append(f"  {BD}TODAY'S SCAN SIGNALS{W}  {DM}(scan: {scan_ts}){W}")
        L.append("")
        L.append(
            f"  {DM}{'Action':<10}  {'Ticker':<7}  {'Strategy':<14}  "
            f"{'Score':>5}  {'Shares':>6}  {'Price':>8}  Reason{W}"
        )
        L.append(HR)
        ACTION_COL = {"BUY": GR + BD, "EXIT": CY, "EXIT(FAILED)": RD, "BLOCKED": DM}
        for a in scan_actions:
            act   = a.get("action", "")
            tk    = (a.get("ticker") or "")[:7]
            strat = (a.get("strategy") or a.get("market_group") or "")[:14]
            score = a.get("score") or 0
            shrs  = a.get("shares") or 0
            price = a.get("price") or 0
            rsn   = (a.get("reason") or "")[:30]
            acol  = ACTION_COL.get(act, DM)
            L.append(
                f"  {acol}{act:<10}{W}  {BD}{tk:<7}{W}  {DM}{strat:<14}{W}  "
                f"{score:>5.2f}  {shrs:>6}  {price:>8.2f}  {DM}{rsn}{W}"
            )
        L.append(HR)
        L.append(
            f"  {GR}{len(buys)} BUY{W}  "
            f"{CY}{len(exits)} EXIT{W}  "
            f"{DM}{len(blocked)} BLOCKED{W}"
        )
        L.append("")

    # ── Trade log ─────────────────────────────────────────────────
    L.append(f"  {BD}TRADE LOG{W}  {DM}(last 15 from trade_log.csv — newest first){W}")
    L.append("")
    L.append(
        f"  {DM}{'Date':<10}  {'Side':<4}  {'Ticker':<7}  {'Strategy':<14}  "
        f"{'Shrs':>5}  {'Price':>8}  {'Value (SEK)':>12}  {'P&L (SEK)':>12}{W}"
    )
    L.append(HR)

    if trades:
        for t in trades:
            act = t["action"]
            if act == "BUY":
                act_s = f"{GR}BUY {W}"
            elif act == "SELL":
                pnl_c = (GR if t["pnl_sek"] and t["pnl_sek"] >= 0 else RD) if t["pnl_sek"] is not None else CY
                act_s = f"{pnl_c}SELL{W}"
            else:
                act_s = f"{DM}{act:<4}{W}"

            pnl_s = (
                f"{'+'if t['pnl_sek']>=0 else ''}{t['pnl_sek']:,.0f}"
                if t["pnl_sek"] is not None else "—"
            )
            pnl_col = (GR if t["pnl_sek"] is not None and t["pnl_sek"] >= 0 else RD) if t["pnl_sek"] is not None else DM
            L.append(
                f"  {act_s}  {t['date']:<10}  {BD}{t['ticker']:<7}{W}  "
                f"{DM}{t['strategy']:<14}  {t['shares']:>5,}  "
                f"{t['price']:>8.2f}  {t['value']:>12,.0f}  "
                f"{pnl_col}{pnl_s:>12}{W}"
            )
    else:
        L.append(f"  {DM}No trades yet.{W}")

    L.append(HR)

    # Stats footer
    closed_c = stats.get("closed", 0) or 0
    wins_c   = stats.get("wins",   0) or 0
    real     = stats.get("realized") or 0
    wr       = wins_c / closed_c * 100 if closed_c else 0
    losses_c = closed_c - wins_c
    L.append(
        f"  {DM}{closed_c} closed  "
        f"{W}W: {GR}{wins_c}{W}  L: {RD}{losses_c}{W}  "
        f"WR: {BD}{wr:.1f}%{W}     "
        f"Realized P&L: {_pnl(real, 'SEK')}"
    )
    L.append("")

    # ── P&L Ledger ────────────────────────────────────────────────
    try:
        import pnl_tracker
        summary = pnl_tracker.get_summary("stock")
        s_pnl   = summary.get("stock", {})
        pnl_r   = s_pnl.get("realized_pnl", 0.0)
        n_cl    = s_pnl.get("closed_trades", 0)
        wr2     = s_pnl.get("win_rate", 0.0)
        pf      = s_pnl.get("profit_factor") or "—"
        best    = s_pnl.get("best_trade", 0.0)
        worst   = s_pnl.get("worst_trade", 0.0)
        pc      = GR if pnl_r >= 0 else RD
        L.append(f"  {BD}P&L LEDGER{W}  {DM}(pnl_ledger.db — run pnl_dashboard.py for full view){W}")
        L.append(HR)
        L.append(
            f"  {BD}Realized P&L:{W}  {pc}{BD}{'+'if pnl_r>=0 else ''}{pnl_r:,.0f} SEK{W}     "
            f"{DM}Closed: {n_cl}  |  WR: {wr2:.1f}%  |  "
            f"Best: +{best:.0f}  |  Worst: {worst:+.0f}  |  PF: {pf}{W}"
        )
        L.append(HR)
    except Exception:
        pass

    # ── Footer ───────────────────────────────────────────────────
    if not once:
        L.append(
            f"  {DM}Refreshes every {interval}s  |  Ctrl+C to exit  |  "
            f"Run: python start_atos.py  to force scan  |  "
            f"FX/Futures: python forex_dashboard.py  /  python futures_dashboard.py{W}"
        )
    L.append("")
    return "\n".join(L)


def main():
    fast     = "--fast" in sys.argv
    once     = "--once" in sys.argv
    interval = 5 if fast else REFRESH_SECONDS

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
