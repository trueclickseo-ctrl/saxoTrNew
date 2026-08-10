"""
terminal_dashboard.py  —  ATOS live portfolio view for PowerShell / cmd
------------------------------------------------------------------------
Usage:
    python terminal_dashboard.py            # refresh every 30s
    python terminal_dashboard.py --fast     # refresh every 5s
    python terminal_dashboard.py --once     # print once and exit
"""

import os, sys, json, time, sqlite3
from datetime import datetime, date, timezone, timedelta

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "data", "atos_live.db")
TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token.json")
SIM_BASE   = "https://gateway.saxobank.com/sim/openapi/"

REFRESH_SECONDS = 30
TRAILING_PCT  = 0.12   # 12% below trailing_stop_high (matches intraday_monitor)
HARD_STOP_PCT = 0.15   # 15% below entry (last resort — US Blend hard floor)
ATR_MULT      = 2.5    # entry − 2.5×ATR  (ATOS policy; used when stop_price stored)

# ── Enable Windows VT100 colour support ───────────────────────────
def _enable_vt():
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # Get stdout handle and enable ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x4)
        handle = kernel32.GetStdHandle(-11)
        mode   = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x4)
    except Exception:
        pass
    # Also works as a secondary trigger on some terminals
    os.system("")

_enable_vt()

# ── Colours ───────────────────────────────────────────────────────
GR  = "\033[92m"    # green
RD  = "\033[91m"    # red
YL  = "\033[93m"    # yellow
BL  = "\033[94m"    # blue
CY  = "\033[96m"    # cyan
MG  = "\033[95m"    # magenta
W   = "\033[0m"     # reset
BD  = "\033[1m"     # bold
DM  = "\033[2m"     # dim
HOME  = "\033[H"
CLEAR = "\033[2J"

REGIME_COL = {"BULL": GR, "BEAR": RD, "SIDEWAYS": YL,
              "TRANSITION": BL, "MOMENTUM": CY}


# ── Token ─────────────────────────────────────────────────────────
def _load_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        d = json.load(open(TOKEN_FILE))
        if time.time() > float(d.get("obtained_at", 0)) + int(d.get("expires_in", 1200)) - 60:
            return None
        return d.get("access_token")
    except Exception:
        return None


# ── Saxo API ──────────────────────────────────────────────────────
def _get(token, path, params=None):
    import requests
    try:
        r = requests.get(SIM_BASE + path,
                         headers={"Authorization": f"Bearer {token}"},
                         params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def _balance(token):
    return _get(token, "port/v1/balances/me")

def _positions(token):
    return _get(token, "port/v1/positions/me",
                {"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"}).get("Data", [])


# ── DB ────────────────────────────────────────────────────────────
def _db_trades():
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

def _db_stats():
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

def _atos_status():
    p = os.path.join(BASE_DIR, "data", "atos_status.json")
    try:
        return json.load(open(p)) if os.path.exists(p) else {"status": "idle"}
    except Exception:
        return {"status": "idle"}


# ── Helpers ───────────────────────────────────────────────────────
def _effective_stop(drow: dict) -> float:
    """Return the effective stop price using the same rules as intraday_monitor.py.
    Priority: 1) stored stop_price  2) trailing stop  3) hard floor."""
    entry       = drow.get("entry_price", 0) or 0
    stop_price  = drow.get("stop_price",  0) or 0
    trail_high  = drow.get("trailing_stop_high") or entry

    if stop_price > 0:
        return stop_price                              # Rule 1 — US Reversion
    if trail_high > 0:
        trail_stop = trail_high * (1.0 - TRAILING_PCT)
        hard_floor = entry * (1.0 - HARD_STOP_PCT)
        return max(trail_stop, hard_floor)             # Rule 2/3 — US Blend
    return entry * (1.0 - HARD_STOP_PCT)              # Rule 3 fallback


def _days_held(entry_date_str: str) -> int:
    """Calendar days since entry_date (ISO string)."""
    try:
        return (date.today() - date.fromisoformat(entry_date_str[:10])).days
    except Exception:
        return 0


def _next_rebalance() -> str:
    """Return the next monthly rebalance date as a short string (e.g. 'Sep 1')."""
    state_path = os.path.join(BASE_DIR, "data", "us_momentum_state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
        last = state.get("last_rebalance")
        if last:
            last_dt = date.fromisoformat(last[:10])
            # Next rebalance: first trading day of the following month
            if last_dt.month == 12:
                nxt = date(last_dt.year + 1, 1, 1)
            else:
                nxt = date(last_dt.year, last_dt.month + 1, 1)
            days_left = (nxt - date.today()).days
            label = nxt.strftime("%b %-d") if sys.platform != "win32" else nxt.strftime("%b %d").lstrip("0")
            return f"{label} ({days_left}d)"
    except Exception:
        pass
    return "monthly"


def _exit_info(drow: dict) -> str:
    """Return a short exit-trigger string for the Exit column."""
    strategy   = (drow.get("strategy") or "").strip()
    entry_date = drow.get("entry_date", "")
    days       = _days_held(entry_date)

    if "Reversion" in strategy:
        # Max 10 trading days; show progress
        try:
            sys.path.insert(0, BASE_DIR)
            from atos.us_reversion import MAX_HOLD_DAYS
        except Exception:
            MAX_HOLD_DAYS = 10
        days_left = max(MAX_HOLD_DAYS - days, 0)
        return f"Day {days}/{MAX_HOLD_DAYS} ({days_left}d left)"
    else:
        # US Blend: monthly rebalance
        return f"Day {days}  Reb: {_next_rebalance()}"


def _usd_sek():
    try:
        sys.path.insert(0, BASE_DIR)
        import fx; return fx.get_rate_to_sek("USD")
    except Exception:
        return 10.95

def _et_now():
    utc = datetime.now(timezone.utc)
    year = utc.year
    mar = datetime(year,3,8,2,tzinfo=timezone.utc)
    while mar.weekday()!=6: mar+=timedelta(days=1)
    nov = datetime(year,11,1,2,tzinfo=timezone.utc)
    while nov.weekday()!=6: nov+=timedelta(days=1)
    tz = timezone(timedelta(hours=-4 if mar<=utc<nov else -5))
    return utc.astimezone(tz)

def _mkt_open():
    et=_et_now(); wk=et.weekday()
    return wk<5 and (9,30)<=(et.hour,et.minute)<(16,0)

def _c(n, fmt=".2f"):
    return f"{n:{fmt}}" if n is not None else "—"

def _pnl(n, suffix=""):
    if n is None: return f"{DM}—{W}"
    c = GR if n>=0 else RD
    s = "+" if n>=0 else ""
    return f"{c}{s}{n:,.0f}{suffix}{W}"

def _pct(n):
    if n is None: return f"{DM}—{W}"
    c = GR if n>=0 else RD
    s = "+" if n>=0 else ""
    return f"{c}{s}{n:.2f}%{W}"

def _reg(r):
    if not r or r=="—": return f"{DM}—{W}"
    return f"{REGIME_COL.get(r.upper(),DM)}{r}{W}"

# strip ANSI for width calculations
def _len(s):
    import re
    return len(re.sub(r'\033\[[0-9;]*m','',s))

def _rpad(s, width):
    pad = width - _len(s)
    return s + " "*max(pad,0)


# ── Render ────────────────────────────────────────────────────────
def render(token):
    L  = []
    HR = f"{DM}{'─'*72}{W}"

    # Header
    et   = _et_now()
    mkt  = f"{GR}● OPEN{W}"  if _mkt_open() else f"{RD}● CLOSED{W}"
    st   = _atos_status()
    s    = st.get("status","idle")
    if s=="running":
        atos = f"{GR}[RUNNING]  ATOS scanning universe...{W}"
    elif s=="complete":
        ts = st.get("timestamp","")[:16].replace("T"," ")
        atos = (f"{BL}[DONE]  Last scan {ts}   "
                f"{GR}{st.get('buy_count',0)} BUY{W}  "
                f"{RD}{st.get('exit_count',0)} EXIT{W}  "
                f"{DM}{st.get('blocked_count',0)} blocked{W}")
    else:
        atos = f"{DM}[IDLE]  Next scan: 2:00 AM PKT{W}"

    L += [
        HR,
        f"  {BD}ATOS  Dashboard{W}              "
        f"{DM}{et.strftime('%A %d %b  %H:%M:%S')} ET{W}   {mkt}",
        f"  {atos}",
        HR,
        "",
    ]

    # Balance
    bal = _balance(token) if token else {}
    cur = bal.get("Currency","EUR")
    eq  = bal.get("TotalValue")
    csh = bal.get("CashBalance")
    upl = bal.get("UnrealizedPositionsValue")

    L.append(f"  {BD}ACCOUNT  ({cur}){W}")
    L.append(f"  {'Total equity':<22} {BD}{_c(eq)}{W}  {cur}")
    L.append(f"  {'Cash available':<22} {_c(csh)}  {cur}")
    L.append(f"  {'Unrealized P&L':<22} {_pnl(upl,'')}")
    L.append("")

    # ── Positions ─────────────────────────────────────────────────
    db    = _db_trades()
    saxo  = _positions(token) if token else []
    acur  = bal.get("Currency", "EUR")   # account currency for P&L label

    # Collect rows as plain dicts first, then render with colour
    rows = []
    total_pnl = 0.0
    count     = 0

    if saxo:
        for p in saxo:
            disp = p.get("DisplayAndFormat", {})
            pb   = p.get("PositionBase", {})
            pv   = p.get("PositionView", {})
            sym  = disp.get("Symbol", "?")
            base = sym.split(":")[0].upper()
            shs  = pb.get("Amount", 0) or 0
            ep   = pb.get("OpenPrice", 0) or 0
            pnl  = pv.get("ProfitLossOnTrade", 0) or 0
            ppc  = pv.get("ProfitLossOnTradeInPercentage", 0) or 0
            live = pv.get("CurrentPrice", 0) or 0
            if live == 0 and shs > 0 and ep > 0:
                live = ep + pnl / shs
            if ppc == 0 and ep > 0 and shs > 0:
                ppc = pnl / (ep * shs) * 100
            drow = db.get(base, {})
            rows.append({
                "ticker": sym.split(":")[0], "shs": int(shs),
                "entry": ep, "live": live, "pnl": pnl, "ppc": ppc,
                "stop": _effective_stop(drow),
                "regime": (drow.get("regime_at_entry") or "—")[:10],
                "strategy": (drow.get("strategy") or "—")[:10],
                "exit_info": _exit_info(drow),
                "live_available": True,
            })
            total_pnl += pnl
            count     += 1
    elif db:
        for base, drow in db.items():
            rows.append({
                "ticker": drow.get("ticker", base).split(":")[0],
                "shs": int(drow.get("shares", 0) or 0),
                "entry": drow.get("entry_price", 0) or 0,
                "live": 0, "pnl": 0, "ppc": 0,
                "stop": _effective_stop(drow),
                "regime": (drow.get("regime_at_entry") or "—")[:10],
                "strategy": (drow.get("strategy") or "—")[:10],
                "exit_info": _exit_info(drow),
                "live_available": False,
            })
            count += 1

    SEP = "  "
    HR2 = f"  {DM}{'─'*96}{W}"

    L.append(f"  {BD}OPEN POSITIONS{W}  {DM}(P&L in {acur}){W}")
    L.append(
        f"{DM}  {'Ticker':<7}{SEP}{'Shrs':>5}{SEP}{'Entry':>8}{SEP}"
        f"{'Live':>8}{SEP}{'Stop Loss':>9}{SEP}{'P&L':>10}{SEP}"
        f"{'Chg%':>7}{SEP}{'Exit Trigger':<22}{SEP}Regime{W}"
    )
    L.append(HR2)

    if not rows:
        L.append(f"  {DM}No open positions{W}")
    else:
        for r in rows:
            tk      = r["ticker"][:7]
            shs     = str(r["shs"])
            ep      = f"{r['entry']:.2f}"
            lv      = f"{r['live']:.2f}" if r["live_available"] else "—"
            st      = f"{r['stop']:.2f}" if r["stop"] else "—"
            ex      = r["exit_info"][:22]

            pnl_raw = r["pnl"]
            ppc_raw = r["ppc"]
            pnl_s   = (("+" if pnl_raw >= 0 else "") + f"{pnl_raw:,.0f}") if r["live_available"] else "—"
            ppc_s   = (("+" if ppc_raw >= 0 else "") + f"{ppc_raw:.2f}%") if r["live_available"] else "—"

            near    = r["stop"] > 0 and r["live"] > 0 and r["live"] < r["stop"] * 1.05
            pnl_col = (GR if pnl_raw >= 0 else RD) if r["live_available"] else DM
            ppc_col = (GR if ppc_raw >= 0 else RD) if r["live_available"] else DM
            stp_col = (RD + BD) if near else YL
            # Reversion positions nearing max hold → warn in yellow
            ex_col  = YL if "0d left" in ex or "1d left" in ex else DM

            L.append(
                f"  {BD}{tk:<7}{W}{SEP}{shs:>5}{SEP}{ep:>8}{SEP}{lv:>8}{SEP}"
                f"{stp_col}{st:>9}{W}{SEP}"
                f"{pnl_col}{pnl_s:>10}{W}{SEP}"
                f"{ppc_col}{ppc_s:>7}{W}{SEP}"
                f"{ex_col}{ex:<22}{W}{SEP}"
                f"{REGIME_COL.get(r['regime'].upper(), DM)}{r['regime']}{W}"
            )

        L.append(HR2)
        total_s   = ("+" if total_pnl >= 0 else "") + f"{total_pnl:,.0f}"
        total_col = GR if total_pnl >= 0 else RD
        L.append(
            f"  {BD}{'TOTAL':<7}{W}{SEP}{count:>5} pos"
            f"{'':>29}"
            f"{total_col}{total_s:>10}{W}"
        )

    # Trade history
    stats = _db_stats()
    if stats:
        closed   = stats.get("closed",0) or 0
        wins     = stats.get("wins",0) or 0
        realized = stats.get("realized") or 0
        wr       = wins/closed*100 if closed else 0
        losses   = closed - wins
        L += [
            "",
            f"  {BD}TRADE HISTORY{W}",
            f"  {'Closed trades':<22} {closed}  "
            f"(W:{GR}{wins}{W}  L:{RD}{losses}{W}  WR: {BD}{wr:.1f}%{W})",
            f"  {'Total realized P&L':<22} {_pnl(realized,' SEK')}",
        ]

    # Footer
    ref = "once" if "--once" in sys.argv else ("5s" if "--fast" in sys.argv else f"{REFRESH_SECONDS}s")
    L += [
        "",
        HR,
        f"  {DM}Refresh: {ref}  │  Updated: {datetime.now().strftime('%H:%M:%S')}  │  Ctrl+C to exit{W}",
        HR,
        "",
    ]
    return "\n".join(L)


# ── Main ──────────────────────────────────────────────────────────
def main():
    global REFRESH_SECONDS
    if "--fast"  in sys.argv: REFRESH_SECONDS = 5
    once     = "--once" in sys.argv
    first    = True

    while True:
        token  = _load_token()
        output = render(token)

        sys.stdout.write((CLEAR+HOME) if first else HOME)
        sys.stdout.write(output)
        sys.stdout.flush()
        first = False

        if once:
            break
        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
            break

if __name__ == "__main__":
    main()
