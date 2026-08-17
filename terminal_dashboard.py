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
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import price_service

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
DB_PATH         = os.path.join(BASE_DIR, "data", "atos_live.db")
TOKEN_FILE      = os.path.join(BASE_DIR, "saxo_token.json")
SIM_BASE        = "https://gateway.saxobank.com/sim/openapi/"
TRADE_LOG_PATH  = os.path.join(BASE_DIR, "data", "trade_log.csv")
ETF_STATE_PATH   = os.path.join(BASE_DIR, "saxo_etf_strategy", "data", "etf_positions.json")
FOREX_STATE_PATH = os.path.join(BASE_DIR, "data", "forex_state.json")
FUTURES_STATE_PATH = os.path.join(BASE_DIR, "data", "futures_state.json")

REFRESH_SECONDS = 30
TRAILING_PCT  = 0.12   # 12% below trailing_stop_high (matches intraday_monitor)
HARD_STOP_PCT = 0.15   # 15% below entry (last resort — US Blend hard floor)
ATR_MULT      = 2.5    # entry − 2.5×ATR  (ATOS policy; used when stop_price stored)

# ── Enable Windows VT100 colour support ───────────────────────────
def _clear_console():
    try:
        import ctypes, struct
        k32  = ctypes.windll.kernel32
        h    = k32.GetStdHandle(-11)
        buf  = ctypes.create_string_buffer(22)
        k32.GetConsoleScreenBufferInfo(h, buf)
        _, _, _, _, _, left, top, right, bottom, _, _ = struct.unpack("hhhhHhhhhhh", buf.raw)
        cols = right - left + 1
        rows = bottom - top + 1
        size = cols * rows
        done = ctypes.c_ulong(0)
        k32.FillConsoleOutputCharacterW(h, 32, size, 0, ctypes.byref(done))
        k32.FillConsoleOutputAttribute(h, 7,  size, 0, ctypes.byref(done))
        k32.SetConsoleCursorPosition(h, 0)
    except Exception:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

try:
    import ctypes as _ct
    _k32 = _ct.windll.kernel32
    _h   = _k32.GetStdHandle(-11)
    _m   = _ct.c_ulong()
    _k32.GetConsoleMode(_h, _ct.byref(_m))
    _k32.SetConsoleMode(_h, _m.value | 0x4)
except Exception:
    pass

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

def _read_stock_trades(n=20):
    """Read last N stock trades from trade_log.csv, newest first."""
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    try:
        import csv
        with open(TRADE_LOG_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        out = []
        for r in rows[-n:]:
            pnl_raw = r.get("pnl_sek", "").strip()
            out.append({
                "date":     r.get("date", "")[:10],
                "action":   r.get("action", "").upper(),
                "ticker":   r.get("ticker", ""),
                "strategy": r.get("strategy", ""),
                "shares":   int(float(r.get("shares") or 0)),
                "price":    float(r.get("price_usd") or 0),
                "value":    float(r.get("value_sek") or 0),
                "pnl":      float(pnl_raw) if pnl_raw else None,
                "currency": "SEK",
                "source":   "stock",
                "dry_run":  False,
            })
        return list(reversed(out))
    except Exception:
        return []


def _etf_live_prices(open_sym_map: dict, token: str = None) -> dict:
    """Fetch ETF live prices: Saxo API first (has UICs), fallback yfinance."""
    if not open_sym_map:
        return {}
    instruments = [
        {"symbol": sym, "uic": int(uic_str), "asset_type": "Etf"}
        for uic_str, pos in _etf_open_positions_raw().items()
        for sym in [pos.get("symbol", "")]
        if sym and sym in open_sym_map
    ]
    if not instruments:
        # plain yfinance fallback if UIC lookup failed
        instruments = [{"symbol": s, "uic": None, "asset_type": "Etf"}
                       for s in open_sym_map]
    prices, _ = price_service.fetch_prices(instruments, token=token)
    return {sym: round(px, 2) for sym, px in prices.items()}


def _etf_open_positions_raw() -> dict:
    """Return raw positions dict {uic_str: {symbol, quantity, entry_price, ...}}."""
    try:
        d = json.load(open(ETF_STATE_PATH, encoding="utf-8"))
        return d.get("positions", {})
    except Exception:
        return {}


def _read_etf_trades():
    """Read ETF order history from etf_positions.json, newest first.
    For open BUY positions, fetches current price via yfinance and shows unrealized P&L."""
    if not os.path.exists(ETF_STATE_PATH):
        return []
    try:
        d      = json.load(open(ETF_STATE_PATH, encoding="utf-8"))
        orders = d.get("orders", [])

        # Determine which symbols are still open (positions dict keyed by UIC)
        open_positions = d.get("positions", {})
        # Build symbol→entry map from positions values
        open_sym_map   = {v["symbol"]: v for v in open_positions.values() if "symbol" in v}
        token          = price_service.load_token()
        live_prices    = _etf_live_prices(open_sym_map, token=token)

        out = []
        for o in reversed(orders):
            side  = (o.get("side") or "").upper()
            sym   = o.get("symbol", "")
            qty   = o.get("quantity", 0)
            entry = o.get("entry_price") if side == "BUY" else o.get("exit_price", 0)
            pnl   = None
            now   = None

            if side == "SELL":
                ep = o.get("entry_price")
                xp = o.get("exit_price")
                if ep and xp:
                    pnl = (xp - ep) * qty
            elif side == "BUY" and sym in open_sym_map and sym in live_prices:
                now = live_prices[sym]
                ep  = o.get("entry_price") or 0
                if ep > 0:
                    pnl = (now - ep) * qty

            out.append({
                "date":      (o.get("timestamp") or "")[:10],
                "action":    side,
                "ticker":    sym,
                "strategy":  "ETF",
                "shares":    qty,
                "price":     entry or 0,
                "now":       now,
                "value":     (entry or 0) * qty,
                "pnl":       pnl,
                "currency":  "USD",
                "source":    "etf",
                "dry_run":   o.get("dry_run", False),
            })
        return out
    except Exception:
        return []


def _read_forex_positions() -> list:
    """Read open FX positions from forex_state.json."""
    if not os.path.exists(FOREX_STATE_PATH):
        return []
    try:
        d         = json.load(open(FOREX_STATE_PATH, encoding="utf-8"))
        positions = d.get("positions", {})
        out = []
        for key, pos in positions.items():
            strat, sym = key.split(":", 1) if ":" in key else ("ema", key)
            out.append({
                "key":        key,
                "strategy":   strat,
                "symbol":     sym,
                "direction":  pos.get("direction", "Buy"),
                "qty":        pos.get("quantity", 0),
                "entry":      float(pos.get("entry_price", 0)),
                "stop":       float(pos.get("stop_price", 0)),
                "entry_date": pos.get("entry_date", ""),
                "atr":        float(pos.get("atr_at_entry", 0)),
            })
        return out
    except Exception:
        return []


def _read_futures_positions() -> list:
    """Read open futures positions from futures_state.json."""
    if not os.path.exists(FUTURES_STATE_PATH):
        return []
    try:
        d         = json.load(open(FUTURES_STATE_PATH, encoding="utf-8"))
        positions = d.get("positions", {})
        out = []
        for key, pos in positions.items():
            strat, sym = key.split(":", 1) if ":" in key else ("donchian", key)
            out.append({
                "key":        key,
                "strategy":   strat,
                "symbol":     sym,
                "direction":  pos.get("direction", "Buy"),
                "qty":        pos.get("quantity", 0),
                "entry":      float(pos.get("entry_price", 0)),
                "stop":       float(pos.get("stop_price", 0)),
                "entry_date": pos.get("entry_date", ""),
            })
        return out
    except Exception:
        return []




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
    """Return the next weekly rebalance date as a short string (e.g. 'Aug 14 (4d)')."""
    from datetime import timedelta
    state_path = os.path.join(BASE_DIR, "data", "us_momentum_state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
        last = state.get("last_rebalance")
        if last:
            last_dt  = date.fromisoformat(last[:10])
            try:
                from atos.us_momentum import REBAL_DAYS
            except Exception:
                REBAL_DAYS = 7
            nxt       = last_dt + timedelta(days=REBAL_DAYS)
            days_left = (nxt - date.today()).days
            label     = nxt.strftime("%b %d").lstrip("0")
            return f"{label} ({days_left}d)"
    except Exception:
        pass
    return "weekly"


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


def _yf_last_close(tickers: list) -> dict:
    """Fetch last closing price from Yahoo Finance for given tickers.
    Returns {ticker: price}. Used when market is closed or Saxo token expired."""
    if not tickers:
        return {}
    try:
        import yfinance as yf
        raw = yf.download(tickers, period="5d", interval="1d",
                          auto_adjust=True, progress=False,
                          group_by="ticker" if len(tickers) > 1 else None)
        result = {}
        if len(tickers) == 1:
            if not raw.empty:
                result[tickers[0]] = float(raw["Close"].dropna().iloc[-1])
        else:
            for t in tickers:
                try:
                    s = raw[t]["Close"].dropna()
                    if not s.empty:
                        result[t] = float(s.iloc[-1])
                except Exception:
                    pass
        return result
    except Exception:
        return {}


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
    status_d = _atos_status()
    st       = status_d   # keep short alias for header block
    s        = st.get("status","idle")
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
        # No Saxo connection — fetch last closing prices from Yahoo Finance
        yf_tickers = [drow.get("ticker", base).split(":")[0] for base, drow in db.items()]
        yf_prices  = _yf_last_close(yf_tickers)
        for base, drow in db.items():
            tk    = drow.get("ticker", base).split(":")[0]
            ep    = drow.get("entry_price", 0) or 0
            shs   = int(drow.get("shares", 0) or 0)
            close = yf_prices.get(tk, 0)
            pnl   = (close - ep) * shs if close and ep and shs else None
            ppc   = ((close - ep) / ep * 100) if close and ep else None
            rows.append({
                "ticker": tk,
                "shs": shs,
                "entry": ep,
                "live": close,
                "pnl": pnl,
                "ppc": ppc,
                "stop": _effective_stop(drow),
                "regime": (drow.get("regime_at_entry") or "—")[:10],
                "strategy": (drow.get("strategy") or "—")[:10],
                "exit_info": _exit_info(drow),
                "live_available": bool(close),
                "is_close_price": True,
            })
            if pnl is not None:
                total_pnl += pnl
            count += 1

    SEP = "  "
    HR2 = f"  {DM}{'─'*96}{W}"

    using_close = any(r.get("is_close_price") for r in rows)
    price_label = "Last Close" if using_close else "Live"
    price_note  = f"  {YL}(last close — market closed){W}" if using_close else ""
    L.append(f"  {BD}OPEN POSITIONS{W}  {DM}(P&L in {acur}){W}{price_note}")
    L.append(
        f"{DM}  {'Ticker':<7}{SEP}{'Shrs':>5}{SEP}{'Entry':>8}{SEP}"
        f"{price_label:>10}{SEP}{'Stop Loss':>9}{SEP}{'P&L':>10}{SEP}"
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
            stop_s  = f"{r['stop']:.2f}" if r["stop"] else "—"
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
                f"{stp_col}{stop_s:>9}{W}{SEP}"
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

    # ── Trade History ─────────────────────────────────────────────
    stock_trades = _read_stock_trades(20)
    etf_trades   = _read_etf_trades()
    TH_HR = f"  {DM}{'─'*96}{W}"
    TH_HDR = (
        f"{DM}  {'Date':<10}  {'Side':<4}  {'Ticker':<7}  {'Strategy':<14}"
        f"  {'Shrs':>6}  {'Entry':>8}  {'Now':>8}  {'Value':>12}  {'P&L':>14}  {'%':>8}{W}"
    )

    def _trade_rows(trades):
        rows = []
        for t in trades:
            act = t["action"]; dry = t["dry_run"]; cur = t["currency"]
            pnl = t["pnl"];    val = t["value"]
            now = t.get("now")  # current live price (ETF open positions only)
            if dry:
                act_lbl = f"{DM}BUY*{W}"
            elif act == "BUY":
                act_lbl = f"{GR}{act:<4}{W}"
            else:
                act_col = (GR if pnl and pnl >= 0 else RD) if pnl is not None else CY
                act_lbl = f"{act_col}{act:<4}{W}"
            if dry:
                pnl_s = f"{DM}[DRY RUN]{W}"
            elif pnl is not None:
                c = GR if pnl >= 0 else RD
                pnl_s = f"{c}{'+'if pnl>=0 else ''}{pnl:,.0f} {cur}{W}"
            else:
                pnl_s = f"{DM}—{W}"
            now_s = f"{now:>8.2f}" if now is not None else f"{'—':>8}"
            if pnl is not None and t["price"] > 0 and t["shares"] > 0:
                pnl_pct = pnl / (t["price"] * t["shares"]) * 100
                ppc = GR if pnl_pct >= 0 else RD
                pct_s = f"{ppc}({'+'if pnl_pct>=0 else ''}{pnl_pct:.2f}%){W}"
            else:
                pct_s = f"{DM}{'':>8}{W}"
            rows.append(
                f"  {act_lbl}  {t['date']:<10}  {BD}{t['ticker']:<7}{W}  "
                f"{DM}{t['strategy']:<14}{W}  {t['shares']:>6,}  {t['price']:>8.2f}"
                f"  {DM}{now_s}{W}  "
                f"{DM}{val:>12,.0f}{W}  {_rpad(pnl_s, 14)}  {pct_s}"
            )
        return rows

    # ── Section 1: Stock trades ────────────────────────────────────
    stats    = _db_stats()
    closed   = stats.get("closed", 0) or 0
    wins     = stats.get("wins", 0)   or 0
    realized = stats.get("realized")  or 0
    wr       = wins / closed * 100 if closed else 0
    losses   = closed - wins

    L += ["", f"  {BD}STOCK TRADES{W}  {DM}(US Blend + US Reversion — last 20){W}"]
    L.append(TH_HR)
    L.append(TH_HDR)
    L.append(TH_HR)
    if stock_trades:
        L += _trade_rows(stock_trades)
    else:
        L.append(f"  {DM}No stock trades yet{W}")
    L.append(TH_HR)
    L.append(
        f"  {closed} closed   "
        f"W:{GR}{wins}{W}  L:{RD}{losses}{W}  WR:{BD}{wr:.1f}%{W}   "
        f"Realized: {_pnl(realized, ' SEK')}"
    )

    # ── Section 2: ETF trades ──────────────────────────────────────
    etf_closed = sum(1 for t in etf_trades if t["action"] == "SELL" and not t["dry_run"])
    etf_open   = sum(1 for t in etf_trades if t["action"] == "BUY"  and not t["dry_run"])
    etf_dry    = sum(1 for t in etf_trades if t["dry_run"])

    # Totals for open positions
    etf_total_cost    = sum(t["value"] for t in etf_trades if t["action"] == "BUY" and not t["dry_run"] and t.get("now") is not None)
    etf_total_now     = sum(t["now"] * t["shares"] for t in etf_trades if t["action"] == "BUY" and not t["dry_run"] and t.get("now") is not None)
    etf_total_pnl     = sum(t["pnl"] for t in etf_trades if t["action"] == "BUY" and not t["dry_run"] and t.get("pnl") is not None)
    etf_realized_pnl  = sum(t["pnl"] for t in etf_trades if t["action"] == "SELL" and not t["dry_run"] and t.get("pnl") is not None)
    etf_pnl_pct       = (etf_total_pnl / etf_total_cost * 100) if etf_total_cost > 0 else 0

    L += ["", f"  {BD}ETF TRADES{W}  {DM}(ETF rotation strategy){W}"]
    L.append(TH_HR)
    L.append(TH_HDR)
    L.append(TH_HR)
    if etf_trades:
        L += _trade_rows(etf_trades)
    else:
        L.append(f"  {DM}No ETF trades yet{W}")
    L.append(TH_HR)

    # Totals row
    if etf_total_cost > 0:
        pnl_col  = GR if etf_total_pnl >= 0 else RD
        pnl_sign = "+" if etf_total_pnl >= 0 else ""
        pct_sign = "+" if etf_pnl_pct  >= 0 else ""
        L.append(
            f"  {BD}{'TOTAL':<38}{W}"
            f"  Cost: {BD}{etf_total_cost:>10,.0f}{W} USD"
            f"  Now: {BD}{etf_total_now:>10,.0f}{W} USD"
            f"  Unrealized P&L: {pnl_col}{BD}{pnl_sign}{etf_total_pnl:,.0f} USD"
            f"  ({pct_sign}{etf_pnl_pct:.2f}%){W}"
        )
    if etf_realized_pnl:
        r_col  = GR if etf_realized_pnl >= 0 else RD
        r_sign = "+" if etf_realized_pnl >= 0 else ""
        L.append(f"  {DM}Realized P&L (closed): {r_col}{r_sign}{etf_realized_pnl:,.0f} USD{W}")

    L.append(
        f"  {etf_open} open   {etf_closed} closed   "
        f"{DM}{etf_dry} dry-run{W}"
    )

    # ── Section 3: Quick Forex + Futures summary ──────────────────────────
    fx_positions  = _read_forex_positions()
    fut_positions = _read_futures_positions()
    MINI_HR = f"  {DM}{'─'*72}{W}"
    L += ["", MINI_HR]
    fx_n  = len(fx_positions)
    fut_n = len(fut_positions)
    L.append(
        f"  {CY}{BD}FOREX{W}  {fx_n} open positions  "
        f"{DM}│{W}  "
        f"{YL}{BD}FUTURES{W}  {fut_n} open positions  "
        f"{DM}  →  run: python forex_dashboard.py  for full FX view{W}"
    )
    L.append(MINI_HR)

    # Today's scan signals
    scan_actions = st.get("actions") or []
    if scan_actions:
        buys     = [a for a in scan_actions if a.get("action") == "BUY"]
        exits    = [a for a in scan_actions if a.get("action") in ("EXIT", "EXIT(FAILED)")]
        blocked  = [a for a in scan_actions if a.get("action") == "BLOCKED"]

        scan_ts  = (st.get("timestamp") or "")[:16].replace("T", " ")
        L += ["", f"  {BD}TODAY'S SIGNALS{W}  {DM}(scan: {scan_ts}){W}"]
        SIG_HR = f"  {DM}{'─'*72}{W}"
        L.append(SIG_HR)

        # Header
        L.append(
            f"{DM}  {'Action':<8}  {'Ticker':<7}  {'Strategy':<12}  "
            f"{'Score':>5}  {'Shrs':>5}  {'Price':>8}  Reason{W}"
        )
        L.append(SIG_HR)

        ACTION_COL = {"BUY": GR+BD, "EXIT": CY, "EXIT(FAILED)": RD, "BLOCKED": DM}
        for a in scan_actions:
            act   = a.get("action", "")
            tk    = (a.get("ticker") or "")[:7]
            strat = (a.get("strategy") or a.get("market_group") or "")[:12]
            score = a.get("score") or 0
            shrs  = a.get("shares") or 0
            price = a.get("price") or 0
            rsn   = (a.get("reason") or "")[:28]
            acol  = ACTION_COL.get(act, DM)
            pnl_a = a.get("pnl_sek")
            pnl_part = (f"  P&L {_pnl(pnl_a,' SEK')}" if pnl_a is not None else "")
            L.append(
                f"  {acol}{act:<8}{W}  {BD}{tk:<7}{W}  {DM}{strat:<12}{W}  "
                f"{score:>5.2f}  {shrs:>5}  {price:>8.2f}  {DM}{rsn}{W}{pnl_part}"
            )

        if not scan_actions:
            L.append(f"  {DM}No signals this scan{W}")

        L.append(SIG_HR)
        L.append(
            f"  {GR}{len(buys)} BUY{W}  "
            f"{CY}{len(exits)} EXIT{W}  "
            f"{DM}{len(blocked)} BLOCKED{W}"
        )

    # ── P&L Summary widget ────────────────────────────────────────────
    try:
        import pnl_tracker
        pnl_s = pnl_tracker.get_summary()
        PNL_HR = f"  {DM}{'─'*80}{W}"
        L += ["", f"  {BD}P&L SUMMARY{W}  {DM}(realized — run pnl_dashboard.py for full view){W}"]
        L.append(PNL_HR)
        MOD_COLS = {"stock": BL, "etf": YL, "futures": MG, "forex": CY}
        row_parts = []
        for mod in ("stock", "etf", "futures", "forex"):
            s    = pnl_s.get(mod, {})
            col  = MOD_COLS.get(mod, DM)
            cur  = "SEK" if mod == "stock" else "USD"
            pnl  = s.get("realized_pnl", 0.0)
            n    = s.get("closed_trades", 0)
            wr   = s.get("win_rate", 0.0)
            pc   = GR if pnl >= 0 else RD
            sign = "+" if pnl >= 0 else ""
            row_parts.append(
                f"{col}{BD}{mod.upper():<8}{W} "
                f"{pc}{BD}{sign}{pnl:>+,.0f}{W} {DM}{cur}  n={n}  WR={wr:.0f}%{W}"
            )
        L.append("  " + "   │   ".join(row_parts))
        # Grand total (USD modules)
        tot  = pnl_s.get("total", {})
        tpnl = tot.get("realized_pnl", 0.0)
        tc   = GR if tpnl >= 0 else RD
        ts   = "+" if tpnl >= 0 else ""
        L.append(
            f"  {DM}Total realized (USD modules):{W}  "
            f"{tc}{BD}{tpnl:>+,.2f} USD{W}   "
            f"{DM}{tot.get('closed_trades',0)} trades  |  WR {tot.get('win_rate',0):.1f}%{W}"
        )
        L.append(PNL_HR)
    except Exception:
        pass

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

        _clear_console()
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
