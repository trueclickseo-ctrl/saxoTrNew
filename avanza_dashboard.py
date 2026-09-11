"""
avanza_dashboard.py  —  Avanza mini futures dashboard
------------------------------------------------------
Shows Paper Trading (SIM) and Live sections with a separator.
Live section is a placeholder until real money moves to Avanza.

Usage:
    python avanza_dashboard.py          # refresh every 60s
    python avanza_dashboard.py --once   # print once and exit
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_ROOT       = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE = os.path.join(_ROOT, "data", "avanza_paper_positions.json")
_LOG_FILE   = os.path.join(_ROOT, "data", "avanza_paper_trading.log")

REFRESH_SECONDS = 60

INSTRUMENTS = {
    "DAX":        {"name": "DAX (Germany)",   "strategy": "reversion", "leverage": 5.4, "budget_sek": 3000.0, "product": "MINI L DAX AVA 850",            "yahoo": "^GDAXI",    "avanza_id": "2037484",  "commission_sek": 0.0},
    "SP500":      {"name": "S&P 500 (US)",    "strategy": "reversion", "leverage": 5.8, "budget_sek": 3000.0, "product": "MINI L SP500 AVA 339",           "yahoo": "^GSPC",     "avanza_id": "2094745",  "commission_sek": 0.0},
    "GOLD":       {"name": "Gold",            "strategy": "trend",     "leverage": 5.0, "budget_sek": 3000.0, "product": "MINI L GULD AVA 247",            "yahoo": "GC=F",      "avanza_id": "2039813",  "commission_sek": 0.0},
    "APPLE":      {"name": "Apple (AAPL)",    "strategy": "trend",     "leverage": 5.2, "budget_sek": 3000.0, "product": "MINI L APPLE AVA 91",            "yahoo": "AAPL",      "avanza_id": "2474069",  "commission_sek": 0.0},
    "GOOGLE":     {"name": "Google (GOOGL)",  "strategy": "trend",     "leverage": 4.7, "budget_sek": 3000.0, "product": "MINI L GOOGLE AVA 63",           "yahoo": "GOOGL",     "avanza_id": "2228507",  "commission_sek": 0.0},
    "ORACLE":       {"name": "Oracle (ORCL)",       "strategy": "trend",     "leverage": 4.0, "budget_sek": 3000.0, "product": "MINI L ORACLE NORDNET SE26",    "yahoo": "ORCL",      "avanza_id": "2576010",  "commission_sek": 0.0},
    "ASTRAZENECA":  {"name": "AstraZeneca (AZN)",   "strategy": "reversion", "leverage": 2.0, "budget_sek": 3000.0, "product": "MINI L ASTRAZENECA AVA 29",    "yahoo": "AZN",       "avanza_id": "1251802",  "commission_sek": 0.0},
    "OMX":          {"name": "OMX Stockholm 30",    "strategy": "reversion", "leverage": 1.3, "budget_sek": 3000.0, "product": "MINI L OMX AVA 5",             "yahoo": "^OMX",      "avanza_id": "564078",   "commission_sek": 0.0},
}

MARKET_OPEN_PKT  = (12, 0)   # 09:00 CET = 12:00 PKT
MARKET_CLOSE_PKT = (20, 30)  # 17:30 CET = 20:30 PKT

# ── Column widths (visual chars — used to compute separator widths) ─────────────
# Paper Trading open-positions row  (indent=2, gap=2)
_P_INST  = 16   # left
_P_STRAT =  9   # left  ("reversion")
_P_LEV   =  5   # right ("5.4x")
_P_ENTRY = 10   # right (price)
_P_DATE  = 10   # right ("2026-09-11")
_P_OPNL  = 22   # right ("+1,234 SEK (+99.9%)")
_P_GAP   =  2
_P_IND   =  2
_P_ROW   = (_P_IND + _P_INST + _P_GAP + _P_STRAT + _P_GAP + _P_LEV
            + _P_GAP + _P_ENTRY + _P_GAP + _P_DATE + _P_GAP + _P_OPNL)
# = 2 + 16 + 2 + 9 + 2 + 5 + 2 + 10 + 2 + 10 + 2 + 22 = 84

# Warranter View row  (indent=2, gap=3)
_W_NAMN  = 22   # left
_W_ANTAL =  5   # right
_W_SEN   =  8   # right ("  132.10")
_W_INKOP = 10   # right ("    153.90")
_W_SEDAN = 19   # right ("-22 kr (-14.2%)")
_W_VARD  =  9   # right ("1,990 kr")
_W_COMM  =  5   # right ("0 kr")
_W_GAP   =  3
_W_IND   =  2
_W_ROW   = (_W_IND + _W_NAMN + _W_GAP + _W_ANTAL + _W_GAP + _W_SEN
            + _W_GAP + _W_INKOP + _W_GAP + _W_SEDAN + _W_GAP + _W_VARD
            + _W_GAP + _W_COMM)
# = 2 + 22 + 3 + 5 + 3 + 8 + 3 + 10 + 3 + 19 + 3 + 9 + 3 + 5 = 99

# Closed-trades row  (indent=2, gap=2)
_C_INST  = 16   # left
_C_TR    =  6   # right
_C_WIN   =  4   # right
_C_LOS   =  5   # right
_C_WR    =  6   # right ("  55%")
_C_PF    =  6   # right (" 1.50")
_C_PNL   = 11   # right
_C_COMM  =  5   # right
_C_GATE  =  9   # right
_C_GAP   =  2
_C_IND   =  2
_C_ROW   = (_C_IND + _C_INST + _C_GAP + _C_TR + _C_GAP + _C_WIN
            + _C_GAP + _C_LOS + _C_GAP + _C_WR + _C_GAP + _C_PF
            + _C_GAP + _C_PNL + _C_GAP + _C_COMM + _C_GAP + _C_GATE)
# = 2+16+2+6+2+4+2+5+2+6+2+6+2+11+2+5+2+9 = 86

W = max(_P_ROW, _W_ROW, _C_ROW) + 2   # 101 — master border width

# ── ANSI colours ───────────────────────────────────────────────────────────────

class C:
    RST     = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RED     = "\033[91m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    BLUE    = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN    = "\033[96m"
    WHITE   = "\033[97m"
    GRAY    = "\033[90m"
    BRED    = BOLD + RED
    BGRN    = BOLD + GREEN
    BYEL    = BOLD + YELLOW
    BCYN    = BOLD + CYAN
    BWHT    = BOLD + WHITE
    BMAG    = BOLD + MAGENTA


def _c(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + C.RST


def _cell(value: str, width: int, left: bool = False, color: str | None = None) -> str:
    """Pad plain text to `width` chars first, THEN apply color.
    Visual width is always exactly `width` regardless of ANSI codes."""
    plain = f"{value:<{width}}" if left else f"{value:>{width}}"
    return _c(plain, color) if color else plain


def _pnl_cell(val: float, width: int, unit: str = "SEK") -> str:
    sign  = "+" if val >= 0 else ""
    plain = f"{sign}{val:,.0f} {unit}"
    pad   = f"{plain:>{width}}"
    col   = C.BGRN if val > 0 else C.BRED if val < 0 else C.GRAY
    return _c(pad, col)


def _sedan_cell(sedan_sek: float, sedan_pct: float, width: int = _W_SEDAN) -> str:
    sign  = "+" if sedan_sek > 0 else "-" if sedan_sek < 0 else ""
    plain = f"{sign}{abs(sedan_sek):,.0f} kr ({sign}{abs(sedan_pct):.1f}%)"
    pad   = f"{plain:>{width}}"
    col   = C.BGRN if sedan_sek > 0 else C.BRED if sedan_sek < 0 else C.GRAY
    return _c(pad, col)


def _wr_color(wr: float) -> str:
    if wr >= 55: return C.BGRN
    if wr >= 40: return C.YELLOW
    return C.RED


def _pf_color(pf: float) -> str:
    if pf >= 1.5: return C.BGRN
    if pf >= 1.0: return C.YELLOW
    return C.RED


# ── helpers ────────────────────────────────────────────────────────────────────

def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def _load_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"positions": {}, "trades": []}


def _now_pkt() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5)


def _market_open() -> bool:
    now = _now_pkt()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    oh, om = MARKET_OPEN_PKT
    ch, cm = MARKET_CLOSE_PKT
    return (h * 60 + m) >= (oh * 60 + om) and (h * 60 + m) < (ch * 60 + cm)


def _last_log_lines(n: int = 5) -> list[str]:
    try:
        with open(_LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []


def _fmt_date(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        dt  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        pkt = dt.astimezone(timezone.utc) + timedelta(hours=5)
        return pkt.strftime("%Y-%m-%d")
    except Exception:
        return ts[:10] if ts else "-"


# ── live prices ────────────────────────────────────────────────────────────────

_price_cache: dict[str, tuple[float, datetime]] = {}
_CACHE_TTL_S = 60


def _fetch_price(yahoo: str) -> float | None:
    now = datetime.now()
    cached = _price_cache.get(yahoo)
    if cached and (now - cached[1]).total_seconds() < _CACHE_TTL_S:
        return cached[0]
    try:
        import yfinance as yf
        df = yf.download(yahoo, period="2d", auto_adjust=True, progress=False)
        if df.empty:
            return None
        closes = df["Close"]
        if hasattr(closes, "squeeze"):
            closes = closes.squeeze()
        price = float(closes.dropna().iloc[-1])
        _price_cache[yahoo] = (price, now)
        return price
    except Exception:
        return None


_avanza_sek_cache: dict[str, tuple[float, datetime]] = {}


def _load_avanza_env() -> None:
    env_file = os.path.join(_ROOT, ".env.avanza")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, _, v = line.partition('=')
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_avanza_client_cache: list = []


def _fetch_avanza_sek(avanza_id: str) -> float | None:
    now = datetime.now()
    cached = _avanza_sek_cache.get(avanza_id)
    if cached and (now - cached[1]).total_seconds() < _CACHE_TTL_S:
        return cached[0]
    try:
        _load_avanza_env()
        sys.path.insert(0, _ROOT)
        from avanza_module import avanza_client as ac
        if not _avanza_client_cache:
            _avanza_client_cache.append(ac.get_client())
        client = _avanza_client_cache[0]
        info = ac.get_stock_price(client, avanza_id)
        price = info.get("price", 0.0)
        if price and price > 0:
            _avanza_sek_cache[avanza_id] = (float(price), now)
            return float(price)
    except Exception:
        pass
    return None


def _current_pnl(pos: dict, current_price: float) -> tuple[float, float, bool]:
    entry_price     = pos["entry_price"]
    financing_entry = pos["financing_entry"]
    budget          = pos["budget_sek"]
    rate            = pos.get("financing_rate", 0.055)
    try:
        entry_dt = datetime.fromisoformat(str(pos["entry_date"]))
        elapsed  = (datetime.now() - entry_dt).days
    except Exception:
        elapsed = 0
    daily_rate    = rate / 252
    financing_now = financing_entry * ((1 + daily_rate) ** elapsed)
    is_ko = current_price <= financing_now
    if is_ko:
        return -budget, -1.0, True
    pos_return = (current_price - financing_now) / (entry_price - financing_entry) - 1.0
    pnl_sek    = round(budget * pos_return, 2)
    return pnl_sek, pos_return, False


# ── render ─────────────────────────────────────────────────────────────────────

def render():
    state  = _load_state()
    trades = state.get("trades", [])
    pos    = state.get("positions", {})
    now    = _now_pkt()
    is_mkt = _market_open()
    mkt_str = _c("OPEN", C.BGRN) if is_mkt else _c("CLOSED", C.GRAY)
    g = " " * _P_GAP   # standard 2-space gap
    w = " " * _W_GAP   # warranter 3-space gap

    # ── HEADER ────────────────────────────────────────────────────────────────
    border = _c("=" * W, C.CYAN)
    print(border)
    print(_c(f"{'AVANZA MINI FUTURES  —  PAPER TRADING SIM':^{W}}", C.BCYN))
    ts_line = f"  {now.strftime('%Y-%m-%d  %H:%M')} PKT   |   Market: "
    print(_c(ts_line, C.GRAY) + mkt_str + _c("   |   6 instruments   |   Budget: 2 000 SEK / instrument", C.GRAY))
    print(border)

    # ── SECTION 1: OPEN POSITIONS ─────────────────────────────────────────────
    print()
    print(_c(f"  {'OPEN POSITIONS':^{_P_ROW - 2}}", C.BYEL))
    sep_p = _c("  " + "-" * (_P_ROW - 2), C.DIM)
    print(sep_p)

    hdr = (
        "  "
        + _cell("Instrument",  _P_INST,  left=True,  color=C.BWHT)  + g
        + _cell("Strategy",    _P_STRAT, left=True,  color=C.BWHT)  + g
        + _cell("Lev",         _P_LEV,               color=C.BWHT)  + g
        + _cell("Entry price", _P_ENTRY,              color=C.BWHT)  + g
        + _cell("Entry date",  _P_DATE,               color=C.BWHT)  + g
        + _cell("Open P&L",    _P_OPNL,               color=C.BWHT)
    )
    print(hdr)
    print(sep_p)

    total_open_pnl   = 0.0
    total_closed_pnl = 0.0
    total_wins       = 0
    total_losses     = 0

    for key, cfg in INSTRUMENTS.items():
        p       = pos.get(key, {})
        in_pos  = p.get("status") == "OPEN" and bool(p.get("entry_price"))
        entry_px = p.get("entry_price", 0.0)
        date_s  = _fmt_date(p.get("entry_date", ""))

        sc      = C.BMAG if cfg["strategy"] == "reversion" else C.BCYN
        name_c  = _cell(cfg["name"],       _P_INST,  left=True, color=C.WHITE)
        strat_c = _cell(cfg["strategy"],   _P_STRAT, left=True, color=sc)
        lev_c   = _cell(f"{cfg['leverage']:.1f}x", _P_LEV, color=C.YELLOW)

        if in_pos:
            entry_c = _cell(f"{entry_px:,.2f}", _P_ENTRY, color=C.WHITE)
            date_c  = _cell(date_s,              _P_DATE,  color=C.BLUE)
            cur = _fetch_price(cfg["yahoo"])
            if cur is not None:
                pnl_sek, pnl_pct, is_ko = _current_pnl(p, cur)
                total_open_pnl += pnl_sek
                if is_ko:
                    opnl_c = _cell(f"KO! -{cfg['budget_sek']:.0f} SEK", _P_OPNL, color=C.BRED)
                else:
                    sign   = "+" if pnl_sek >= 0 else ""
                    plain  = f"{sign}{pnl_sek:,.0f} SEK  ({sign}{pnl_pct*100:.1f}%)"
                    opnl_c = _cell(plain, _P_OPNL, color=C.BGRN if pnl_sek >= 0 else C.BRED)
            else:
                opnl_c = _cell("no data", _P_OPNL, color=C.GRAY)
        else:
            entry_c = _cell("-", _P_ENTRY, color=C.GRAY)
            date_c  = _cell("-", _P_DATE,  color=C.GRAY)
            opnl_c  = _cell("-", _P_OPNL,  color=C.GRAY)

        print("  " + name_c + g + strat_c + g + lev_c + g + entry_c + g + date_c + g + opnl_c)

    print(sep_p)
    sign = "+" if total_open_pnl >= 0 else ""
    col  = C.BGRN if total_open_pnl > 0 else C.BRED if total_open_pnl < 0 else C.GRAY
    print("  " + _cell("", _P_INST + _P_GAP + _P_STRAT + _P_GAP + _P_LEV + _P_GAP + _P_ENTRY + _P_GAP + _P_DATE, left=True)
          + g + _cell(f"{sign}{total_open_pnl:,.0f} SEK  total open", _P_OPNL, color=col))

    # ── SECTION 2: AVANZA WARRANTER VIEW ─────────────────────────────────────
    open_positions = {k: v for k, v in pos.items()
                      if v.get("status") == "OPEN" and v.get("entry_sek")}
    if open_positions:
        print()
        print()
        # box top
        box_inner = _W_ROW - 2          # inner width inside "  │ ... │"
        box_line  = "─" * (box_inner + 2)
        print(_c(f"  ┌{box_line}┐", C.YELLOW))
        title = "AVANZA WARRANTER VIEW"
        print(_c(f"  │ {title:^{box_inner}} │", C.BYEL))
        print(_c(f"  ├{box_line}┤", C.YELLOW))

        # header row inside box
        hdr_w = (
            "   "
            + _cell("Naam",       _W_NAMN,  left=True, color=C.BWHT) + w
            + _cell("Antal",      _W_ANTAL,             color=C.BWHT) + w
            + _cell("Senast",     _W_SEN,               color=C.BWHT) + w
            + _cell("Inkopskurs", _W_INKOP,             color=C.BWHT) + w
            + _cell("Sedan kop",  _W_SEDAN,             color=C.BWHT) + w
            + _cell("Varde",      _W_VARD,              color=C.BWHT) + w
            + _cell("Comm",       _W_COMM,              color=C.BWHT)
            + "  │"
        )
        print(_c("  │", C.YELLOW) + hdr_w)
        inner_sep = _c("  │", C.YELLOW) + _c(" " + "-" * box_inner + " ", C.DIM) + _c("│", C.YELLOW)
        print(inner_sep)

        for key, p in open_positions.items():
            cfg       = INSTRUMENTS.get(key, {})
            namn      = cfg.get("product", key)[:_W_NAMN]
            qty       = p.get("qty", 0)
            entry_sek = p.get("entry_sek", 0.0)
            comm_sek  = cfg.get("commission_sek", 0.0)
            avanza_id = cfg.get("avanza_id", "")
            senast    = _fetch_avanza_sek(avanza_id) if avanza_id else None

            namn_c  = _cell(namn, _W_NAMN, left=True, color=C.CYAN)
            antal_c = _cell(str(qty), _W_ANTAL, color=C.WHITE)
            inkop_c = _cell(f"{entry_sek:.2f}", _W_INKOP, color=C.WHITE)
            comm_c  = _cell(f"{comm_sek:.0f} kr", _W_COMM, color=C.GRAY)

            if senast and senast > 0:
                sedan_sek  = (senast - entry_sek) * qty
                sedan_pct  = (senast - entry_sek) / entry_sek * 100 if entry_sek else 0.0
                varde      = senast * qty
                varde_col  = C.BGRN if sedan_sek >= 0 else C.BRED
                sen_c      = _cell(f"{senast:.2f}", _W_SEN,  color=C.WHITE)
                sedan_c    = _sedan_cell(sedan_sek, sedan_pct)
                varde_c    = _cell(f"{varde:,.0f} kr", _W_VARD, color=varde_col)
            else:
                sen_c   = _cell("N/A", _W_SEN,  color=C.GRAY)
                sedan_c = _cell("N/A", _W_SEDAN, color=C.GRAY)
                varde_c = _cell("N/A", _W_VARD,  color=C.GRAY)

            row = "   " + namn_c + w + antal_c + w + sen_c + w + inkop_c + w + sedan_c + w + varde_c + w + comm_c + "  │"
            print(_c("  │", C.YELLOW) + row)

        # footnote inside box
        note = "  AVA-branded instruments:  0 kr brokerage  (financing cost is implicit in daily drift)"
        print(_c("  │", C.YELLOW) + _c(f" {note:<{box_inner}} │", C.GRAY + C.DIM))
        print(_c(f"  └{box_line}┘", C.YELLOW))

    # ── SECTION 3: PAPER TRADE HISTORY ────────────────────────────────────────
    print()
    print()
    print(_c(f"  {'PAPER TRADE HISTORY':^{_C_ROW - 2}}", C.BYEL))
    sep_c = _c("  " + "-" * (_C_ROW - 2), C.DIM)
    print(sep_c)

    hdr_c = (
        "  "
        + _cell("Instrument",  _C_INST, left=True, color=C.BWHT) + g
        + _cell("Trades",      _C_TR,              color=C.BWHT) + g
        + _cell("Wins",        _C_WIN,             color=C.BWHT) + g
        + _cell("Loss",        _C_LOS,             color=C.BWHT) + g
        + _cell("WR",          _C_WR,              color=C.BWHT) + g
        + _cell("PF",          _C_PF,              color=C.BWHT) + g
        + _cell("Closed P&L",  _C_PNL,             color=C.BWHT) + g
        + _cell("Comm",        _C_COMM,            color=C.BWHT) + g
        + _cell("Gate",        _C_GATE,            color=C.BWHT)
    )
    print(hdr_c)
    print(sep_c)

    N_GATE          = 5
    total_gross_win = 0.0
    total_gross_loss= 0.0
    total_comm      = 0.0

    for key, cfg in INSTRUMENTS.items():
        inst_trades = [t for t in trades if t.get("instrument") == key]
        n      = len(inst_trades)
        wins   = sum(1 for t in inst_trades if t.get("pnl_sek", t.get("pnl", 0)) > 0)
        losses = n - wins
        wr     = (wins / n * 100) if n else 0.0
        pnl    = sum(t.get("pnl_sek", t.get("pnl", 0)) for t in inst_trades)
        comm   = sum(t.get("commission_sek", cfg.get("commission_sek", 0.0)) for t in inst_trades)
        gw     = sum(t.get("pnl_sek", t.get("pnl", 0)) for t in inst_trades if t.get("pnl_sek", t.get("pnl", 0)) > 0)
        gl     = abs(sum(t.get("pnl_sek", t.get("pnl", 0)) for t in inst_trades if t.get("pnl_sek", t.get("pnl", 0)) <= 0))
        pf     = (gw / gl) if gl > 0 else (float("inf") if gw > 0 else 0.0)
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"

        remaining = max(0, N_GATE - n)
        gate_s    = "READY" if not remaining else f"{remaining} more"
        gate_col  = C.BGRN if not remaining else C.YELLOW

        total_closed_pnl += pnl
        total_wins       += wins
        total_losses     += losses
        total_gross_win  += gw
        total_gross_loss += gl
        total_comm       += comm

        pnl_sign = "+" if pnl >= 0 else ""
        pnl_plain = f"{pnl_sign}{pnl:,.0f} kr"
        pnl_col   = C.BGRN if pnl > 0 else C.BRED if pnl < 0 else C.GRAY

        print(
            "  "
            + _cell(cfg["name"], _C_INST, left=True, color=C.WHITE) + g
            + _cell(str(n),      _C_TR,               color=C.WHITE) + g
            + _cell(str(wins),   _C_WIN,              color=C.GREEN) + g
            + _cell(str(losses), _C_LOS,              color=C.RED  ) + g
            + _cell(f"{wr:.0f}%",_C_WR,              color=_wr_color(wr)) + g
            + _cell(pf_str,      _C_PF,               color=_pf_color(pf)) + g
            + _cell(pnl_plain,   _C_PNL,              color=pnl_col) + g
            + _cell(f"{comm:.0f} kr", _C_COMM,        color=C.GRAY) + g
            + _cell(gate_s,      _C_GATE,             color=gate_col)
        )

    total_n   = total_wins + total_losses
    total_wr  = (total_wins / total_n * 100) if total_n else 0.0
    total_pf  = (total_gross_win / total_gross_loss) if total_gross_loss > 0 else (float("inf") if total_gross_win > 0 else 0.0)
    tpf_str   = f"{total_pf:.2f}" if total_pf != float("inf") else "inf"
    tpnl_sign = "+" if total_closed_pnl >= 0 else ""
    tpnl_plain = f"{tpnl_sign}{total_closed_pnl:,.0f} kr"

    print(sep_c)
    print(
        "  "
        + _cell("TOTAL",         _C_INST, left=True, color=C.BWHT) + g
        + _cell(str(total_n),    _C_TR,               color=C.WHITE) + g
        + _cell(str(total_wins), _C_WIN,              color=C.GREEN) + g
        + _cell(str(total_losses),_C_LOS,             color=C.RED  ) + g
        + _cell(f"{total_wr:.0f}%", _C_WR,           color=_wr_color(total_wr)) + g
        + _cell(tpf_str,         _C_PF,               color=_pf_color(total_pf)) + g
        + _cell(tpnl_plain,      _C_PNL,              color=C.BGRN if total_closed_pnl > 0 else C.BRED if total_closed_pnl < 0 else C.GRAY) + g
        + _cell(f"{total_comm:.0f} kr", _C_COMM,      color=C.GRAY) + g
        + _cell("",              _C_GATE)
    )

    # ── LIVE SEPARATOR ────────────────────────────────────────────────────────
    print()
    print()
    print(_c(f"  {'─' * (_P_ROW - 2)}", C.BLUE + C.DIM))
    print(_c(f"  {'· · ·  AVANZA LIVE  —  not yet active  · · ·':^{_P_ROW - 2}}", C.BLUE + C.BOLD))
    print(_c(f"  {'─' * (_P_ROW - 2)}", C.BLUE + C.DIM))
    print()
    print(_c("  Live trading unlocks when each instrument reaches 5 closed paper trades with positive total P&L.", C.GRAY))
    print()
    print(_c("  Gate status:", C.BWHT))
    for key, cfg in INSTRUMENTS.items():
        inst_trades = [t for t in trades if t.get("instrument") == key]
        n    = len(inst_trades)
        pnl  = sum(t.get("pnl_sek", t.get("pnl", 0)) for t in inst_trades)
        done = n >= N_GATE and pnl > 0
        bar  = _c("#" * n, C.BGRN) + _c("." * max(0, N_GATE - n), C.GRAY)
        flag = _c("READY", C.BGRN) if done else _c(f"{max(0, N_GATE - n)} to go", C.YELLOW)
        nm   = f"{cfg['name']:<16}"
        sign_g = "+" if pnl >= 0 else ""
        pnl_g  = _c(f"{sign_g}{pnl:,.0f} SEK", C.BGRN if pnl > 0 else C.BRED if pnl < 0 else C.GRAY)
        print(f"    {_c(nm, C.WHITE)}  [{bar}]  {n}/{N_GATE}  {pnl_g}   {flag}")

    # ── RECENT ACTIVITY ───────────────────────────────────────────────────────
    print()
    print(_c(f"  {'─' * (_P_ROW - 2)}", C.DIM))
    print(_c("  Recent activity:", C.BWHT))
    for line in _last_log_lines(5):
        print(_c(f"    {line[:W - 6]}", C.GRAY))

    print()
    print(_c("  Scheduler: 13:30 / 19:30 / 20:00 PKT  |  Market: Mon-Fri  12:00-20:30 PKT", C.GRAY))
    print(_c(f"  {_STATE_FILE}", C.GRAY + C.DIM))
    print(_c("=" * W, C.CYAN))


def main():
    once = "--once" in sys.argv
    while True:
        _clear()
        render()
        if once:
            break
        print(_c(f"\n  Refreshing every {REFRESH_SECONDS}s  (Ctrl+C to exit)", C.GRAY + C.DIM))
        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
