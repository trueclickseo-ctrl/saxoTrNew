"""
terminal_dashboard.py
----------------------
Live PowerShell terminal dashboard for ATOS.
Shows the same data as localhost:8070 — account balance, open positions,
P&L, stop loss, regime — but rendered in the terminal with colour.

Refreshes every REFRESH_SECONDS (default 30).

Usage:
    python terminal_dashboard.py            # refresh every 30s
    python terminal_dashboard.py --fast     # refresh every 5s
    python terminal_dashboard.py --once     # print once and exit
"""

import os
import sys
import json
import time
import sqlite3
from datetime import datetime, timezone, timedelta

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, "data", "atos_live.db")
TOKEN_FILE = os.path.join(BASE_DIR, "saxo_token.json")
SIM_BASE  = "https://gateway.saxobank.com/sim/openapi/"

REFRESH_SECONDS = 30

# ── ANSI colours ──────────────────────────────────────────────────
G  = "\033[92m"   # green
R  = "\033[91m"   # red
Y  = "\033[93m"   # yellow
B  = "\033[94m"   # blue
C  = "\033[96m"   # cyan
W  = "\033[0m"    # reset
BD = "\033[1m"    # bold
DIM = "\033[2m"   # dim

HOME  = "\033[H"
CLEAR = "\033[2J"

REGIME_COLOUR = {
    "BULL": G, "BEAR": R, "SIDEWAYS": Y,
    "TRANSITION": B, "MOMENTUM": C,
}


# ── Token ─────────────────────────────────────────────────────────

def _load_token():
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE) as f:
            d = json.load(f)
        tok = d.get("access_token", "")
        obtained = float(d.get("obtained_at", 0))
        expires  = int(d.get("expires_in", 1200))
        if time.time() > obtained + expires - 60:
            return None
        return tok
    except Exception:
        return None


# ── Saxo API ──────────────────────────────────────────────────────

def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _get_balance(token):
    import requests
    try:
        r = requests.get(SIM_BASE + "port/v1/balances/me",
                         headers=_headers(token), timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return {}


def _get_positions(token):
    import requests
    try:
        r = requests.get(SIM_BASE + "port/v1/positions/me",
                         headers=_headers(token),
                         params={"FieldGroups": "PositionBase,PositionView,DisplayAndFormat"},
                         timeout=10)
        if r.status_code == 200:
            return r.json().get("Data", [])
    except Exception:
        pass
    return []


# ── DB helpers ────────────────────────────────────────────────────

def _db_open_trades():
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM trades WHERE exit_date IS NULL")
        rows = cur.fetchall()
        conn.close()
        result = {}
        for row in rows:
            base = (row["ticker"] or "").split(".")[0].split(":")[0].upper()
            result[base] = dict(row)
        return result
    except Exception:
        return {}


def _db_summary():
    if not os.path.exists(DB_PATH):
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE exit_date IS NULL)     AS open_count,
                COUNT(*) FILTER (WHERE exit_date IS NOT NULL) AS closed_count,
                SUM(pnl_sek) FILTER (WHERE exit_date IS NOT NULL) AS total_realized,
                COUNT(*) FILTER (WHERE exit_date IS NOT NULL AND pnl_sek > 0) AS wins,
                COUNT(*) FILTER (WHERE exit_date IS NOT NULL AND pnl_sek <= 0) AS losses
            FROM trades
        """)
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception:
        return {}


def _atos_status():
    path = os.path.join(BASE_DIR, "data", "atos_status.json")
    if not os.path.exists(path):
        return {"status": "idle"}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"status": "idle"}


# ── FX (simple fallback if fx module unavailable) ─────────────────

def _usd_to_sek():
    try:
        sys.path.insert(0, BASE_DIR)
        import fx
        return fx.get_rate_to_sek("USD")
    except Exception:
        return 10.95


# ── ET market hours ───────────────────────────────────────────────

def _et_now():
    now_utc = datetime.now(timezone.utc)
    year = now_utc.year
    mar  = datetime(year, 3, 8,  2, tzinfo=timezone.utc)
    while mar.weekday() != 6: mar += timedelta(days=1)
    nov  = datetime(year, 11, 1, 2, tzinfo=timezone.utc)
    while nov.weekday() != 6: nov += timedelta(days=1)
    tz = timezone(timedelta(hours=-4)) if mar <= now_utc < nov else timezone(timedelta(hours=-5))
    return now_utc.astimezone(tz)


def _market_open():
    et = _et_now()
    if et.weekday() >= 5:
        return False
    t = (et.hour, et.minute)
    return (9, 30) <= t < (16, 0)


# ── Rendering ─────────────────────────────────────────────────────

def _fmt(n, decimals=2):
    if n is None: return "—"
    return f"{n:,.{decimals}f}"

def _pnl_str(n, currency=""):
    if n is None: return f"{DIM}—{W}"
    col = G if n >= 0 else R
    sign = "+" if n >= 0 else ""
    return f"{col}{sign}{n:,.0f}{(' ' + currency) if currency else ''}{W}"

def _pct_str(n):
    if n is None: return f"{DIM}—{W}"
    col = G if n >= 0 else R
    sign = "+" if n >= 0 else ""
    return f"{col}{sign}{n:.2f}%{W}"

def _regime_str(regime):
    if not regime or regime == "—":
        return f"{DIM}—{W}"
    col = REGIME_COLOUR.get(str(regime).upper(), DIM)
    return f"{col}{regime}{W}"


def render(token):
    lines = []
    W80 = "─" * 82

    now_et   = _et_now()
    mkt_str  = f"{G}OPEN{W}" if _market_open() else f"{R}CLOSED{W}"
    status   = _atos_status()
    st       = status.get("status", "idle")
    if st == "running":
        st_str = f"{G}▶ ATOS RUNNING{W}"
    elif st == "complete":
        ts = status.get("timestamp", "")[:16].replace("T", " ")
        st_str = (f"{B}✓ Last scan {ts} — "
                  f"{status.get('buy_count',0)} BUY · "
                  f"{status.get('exit_count',0)} EXIT{W}")
    else:
        st_str = f"{DIM}ATOS idle — next scan 2:00 AM PKT{W}"

    lines.append(W80)
    lines.append(f"  {BD}ATOS Terminal Dashboard{W}  │  "
                 f"{now_et.strftime('%A %H:%M:%S')} ET  │  Market: {mkt_str}")
    lines.append(f"  {st_str}")
    lines.append(W80)

    # ── Account balance ──────────────────────────────────────────
    bal = _get_balance(token) if token else {}
    if bal:
        cash     = bal.get("CashBalance", 0) or 0
        equity   = bal.get("TotalValue",  0) or 0
        currency = bal.get("Currency",  "?")
        unreal   = bal.get("UnrealizedPositionsValue", 0) or 0
        lines.append(f"\n  {BD}ACCOUNT BALANCE ({currency}){W}")
        lines.append(f"  {'Total Equity':<20} {BD}{_fmt(equity)}{W} {currency}")
        lines.append(f"  {'Cash Available':<20} {_fmt(cash)} {currency}")
        lines.append(f"  {'Unrealized P&L':<20} {_pnl_str(unreal, currency)}")
    else:
        lines.append(f"\n  {Y}[Balance unavailable — token expired or no connection]{W}")

    # ── Open positions ───────────────────────────────────────────
    db_trades = _db_open_trades()
    positions = _get_positions(token) if token else []
    usd_sek   = _usd_to_sek()

    lines.append("")
    lines.append(f"  {BD}OPEN POSITIONS{W}")
    lines.append(f"  {'Ticker':<8}  {'Shares':>6}  {'Entry':>9}  "
                 f"{'Current':>9}  {'Stop Loss':>9}  "
                 f"{'P&L':>10}  {'%Chg':>7}  Regime")
    lines.append("  " + "─" * 78)

    total_pnl = 0.0
    total_cost = 0.0
    pos_count = 0

    if not positions and not db_trades:
        lines.append(f"  {DIM}No open positions{W}")
    elif not positions:
        # Market closed — show DB trades only, no live prices
        for base, t in db_trades.items():
            ticker  = t.get("ticker", base)
            shares  = t.get("shares", 0) or 0
            entry   = t.get("entry_price", 0) or 0
            stop_p  = t.get("stop_price", 0) or 0
            regime  = t.get("regime_at_entry") or "—"
            lines.append(
                f"  {ticker:<8}  {int(shares):>6}  {_fmt(entry):>9}  "
                f"{'—':>9}  {(_fmt(stop_p) if stop_p else '—'):>9}  "
                f"{'—':>10}  {'—':>7}  {_regime_str(regime)}"
            )
            total_cost += entry * shares * usd_sek
            pos_count  += 1
    else:
        for p in positions:
            disp   = p.get("DisplayAndFormat", {})
            pbase  = p.get("PositionBase", {})
            pview  = p.get("PositionView",  {})
            sym    = disp.get("Symbol", "?")
            base   = sym.split(":")[0].upper()
            shares = pbase.get("Amount", 0) or 0
            entry  = pbase.get("OpenPrice", 0) or 0
            pnl    = pview.get("ProfitLossOnTrade", 0) or 0
            pnl_pct= pview.get("ProfitLossOnTradeInPercentage", 0) or 0
            cur    = pview.get("CurrentPrice", 0) or 0
            if cur == 0 and shares > 0 and entry > 0:
                cur = entry + (pnl / shares)
            if pnl_pct == 0 and entry > 0 and shares > 0:
                pnl_pct = pnl / (entry * shares) * 100

            db = db_trades.get(base, {})
            stop_p  = db.get("stop_price") or 0
            regime  = db.get("regime_at_entry") or "—"

            near_stop = stop_p > 0 and cur > 0 and cur < stop_p * 1.05
            stop_str  = f"{R}{_fmt(stop_p)}{W}" if near_stop else (
                        _fmt(stop_p) if stop_p else f"{DIM}—{W}")

            cur_str = _fmt(cur)
            lines.append(
                f"  {sym.split(':')[0]:<8}  {int(shares):>6}  {_fmt(entry):>9}  "
                f"{cur_str:>9}  {stop_str:>9}  "
                f"{_pnl_str(pnl):>10}  {_pct_str(pnl_pct):>7}  {_regime_str(regime)}"
            )
            total_pnl  += pnl
            total_cost += entry * shares
            pos_count  += 1

    if pos_count > 0:
        lines.append("  " + "─" * 78)
        lines.append(
            f"  {'TOTAL':<8}  {pos_count:>6} pos  {'':>9}  {'':>9}  {'':>9}  "
            f"{_pnl_str(total_pnl):>10}  {'':>7}"
        )

    # ── DB stats ─────────────────────────────────────────────────
    stats = _db_summary()
    if stats:
        closed = stats.get("closed_count", 0) or 0
        wins   = stats.get("wins", 0) or 0
        losses = stats.get("losses", 0) or 0
        wr     = wins / closed * 100 if closed > 0 else 0
        realized = stats.get("total_realized") or 0
        lines.append("")
        lines.append(f"  {BD}TRADE HISTORY{W}")
        lines.append(f"  {'Closed trades':<20} {closed}  "
                     f"(W:{wins} L:{losses}  WR: {_fmt(wr, 1)}%)")
        lines.append(f"  {'Total realized P&L':<20} {_pnl_str(realized, 'SEK')}")

    # ── Footer ───────────────────────────────────────────────────
    lines.append("")
    lines.append(W80)
    refresh_label = "once" if "--once" in sys.argv else (
        "5s" if "--fast" in sys.argv else f"{REFRESH_SECONDS}s")
    lines.append(f"  Refresh: {refresh_label}  │  "
                 f"Updated: {datetime.now().strftime('%H:%M:%S')}  │  "
                 f"Ctrl+C to exit")
    lines.append(W80)
    lines.append("")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────

def main():
    global REFRESH_SECONDS
    if "--fast" in sys.argv:
        REFRESH_SECONDS = 5
    once = "--once" in sys.argv

    initialized = False

    while True:
        token = _load_token()
        if not token:
            print(f"{Y}[WARN] Saxo token expired or missing. "
                  f"Run: python set_token.py{W}")

        output = render(token)

        if not initialized:
            sys.stdout.write(CLEAR + HOME)
            initialized = True
        else:
            sys.stdout.write(HOME)

        sys.stdout.write(output)
        sys.stdout.flush()

        if once:
            break

        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            print("\nDashboard stopped.")
            break


if __name__ == "__main__":
    main()
