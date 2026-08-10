"""
terminal_dashboard.py  —  ATOS live portfolio view for PowerShell / cmd
------------------------------------------------------------------------
Usage:
    python terminal_dashboard.py            # refresh every 30s
    python terminal_dashboard.py --fast     # refresh every 5s
    python terminal_dashboard.py --once     # print once and exit
"""

import os, sys, json, time, sqlite3
from datetime import datetime, timezone, timedelta

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "data", "atos_live.db")
TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token.json")
SIM_BASE   = "https://gateway.saxobank.com/sim/openapi/"

REFRESH_SECONDS = 30

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
        atos = f"{GR}▶  ATOS IS RUNNING  —  Scanning universe...{W}"
    elif s=="complete":
        ts = st.get("timestamp","")[:16].replace("T"," ")
        atos = (f"{BL}✓  Last scan {ts}   "
                f"{GR}{st.get('buy_count',0)} BUY{W}  "
                f"{RD}{st.get('exit_count',0)} EXIT{W}  "
                f"{DM}{st.get('blocked_count',0)} blocked{W}")
    else:
        atos = f"{DM}ATOS idle  —  next scan 2:00 AM PKT{W}"

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

    # Positions
    db    = _db_trades()
    saxo  = _positions(token) if token else []

    L.append(f"  {BD}OPEN POSITIONS{W}")
    # Column headers
    hdr = (f"  {'Ticker':<8}  {'Shrs':>5}  {'Entry':>8}  "
           f"{'Live':>8}  {'Stop':>8}  {'P&L':>9}  {'Chg':>7}  {'Regime':<12}  Strategy")
    L.append(f"{DM}{hdr}{W}")
    L.append(f"  {DM}{'─'*70}{W}")

    total_pnl = 0.0
    count     = 0

    if saxo:
        for p in saxo:
            disp = p.get("DisplayAndFormat",{})
            pb   = p.get("PositionBase",{})
            pv   = p.get("PositionView",{})
            sym  = disp.get("Symbol","?")
            base = sym.split(":")[0].upper()
            shs  = pb.get("Amount",0) or 0
            ep   = pb.get("OpenPrice",0) or 0
            pnl  = pv.get("ProfitLossOnTrade",0) or 0
            ppc  = pv.get("ProfitLossOnTradeInPercentage",0) or 0
            cur  = pv.get("CurrentPrice",0) or 0
            if cur==0 and shs>0 and ep>0: cur = ep + pnl/shs
            if ppc==0 and ep>0 and shs>0: ppc = pnl/(ep*shs)*100

            drow  = db.get(base,{})
            stop  = drow.get("stop_price") or 0
            reg   = drow.get("regime_at_entry") or "—"
            strat = (drow.get("strategy") or "—")[:14]

            near  = stop>0 and cur>0 and cur < stop*1.05
            stop_s = f"{RD}{_c(stop)}{W}" if near else (_c(stop) if stop else f"{DM}—{W}")

            col_pnl = _rpad(_pnl(pnl),14)
            col_pct = _rpad(_pct(ppc),12)
            col_reg = _rpad(_reg(reg),20)

            L.append(
                f"  {sym.split(':')[0]:<8}  {int(shs):>5}  {_c(ep):>8}  "
                f"{_c(cur):>8}  {_rpad(stop_s,8)}  "
                f"{col_pnl}  {col_pct}  {col_reg}  {DM}{strat}{W}"
            )
            total_pnl += pnl
            count     += 1

    elif db:
        # Market closed — DB only, no live prices
        for base, drow in db.items():
            tk    = drow.get("ticker", base).split(":")[0]
            shs   = drow.get("shares",0) or 0
            ep    = drow.get("entry_price",0) or 0
            stop  = drow.get("stop_price") or 0
            reg   = drow.get("regime_at_entry") or "—"
            strat = (drow.get("strategy") or "—")[:14]
            L.append(
                f"  {tk:<8}  {int(shs):>5}  {_c(ep):>8}  "
                f"{'—':>8}  {(_c(stop) if stop else '—'):>8}  "
                f"{'—':>9}  {'—':>7}  {_rpad(_reg(reg),20)}  {DM}{strat}{W}"
            )
            count += 1
    else:
        L.append(f"  {DM}No open positions{W}")

    if count>0:
        L.append(f"  {DM}{'─'*70}{W}")
        L.append(
            f"  {'TOTAL':<8}  {count:>5} pos"
            f"  {'':>8}  {'':>8}  {'':>8}  "
            f"{_rpad(_pnl(total_pnl),14)}"
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
