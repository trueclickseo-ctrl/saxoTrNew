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
    "DAX":        {"name": "DAX (Germany)",   "strategy": "reversion", "leverage": 5.4, "budget_sek": 2000.0, "product": "MINI L DAX AVA 850",            "yahoo": "^GDAXI",    "avanza_id": "2037484",  "commission_sek": 0.0},
    "SP500":      {"name": "S&P 500 (US)",    "strategy": "reversion", "leverage": 5.8, "budget_sek": 2000.0, "product": "MINI L SP500 AVA 339",           "yahoo": "^GSPC",     "avanza_id": "2094745",  "commission_sek": 0.0},
    "GOLD":       {"name": "Gold",            "strategy": "trend",     "leverage": 5.0, "budget_sek": 2000.0, "product": "MINI L GULD AVA 247",            "yahoo": "GC=F",      "avanza_id": "2039813",  "commission_sek": 0.0},
    "APPLE":      {"name": "Apple (AAPL)",    "strategy": "trend",     "leverage": 5.2, "budget_sek": 2000.0, "product": "MINI L APPLE AVA 91",            "yahoo": "AAPL",      "avanza_id": "2474069",  "commission_sek": 0.0},
    "GOOGLE":     {"name": "Google (GOOGL)",  "strategy": "trend",     "leverage": 4.7, "budget_sek": 2000.0, "product": "MINI L GOOGLE AVA 63",           "yahoo": "GOOGL",     "avanza_id": "2228507",  "commission_sek": 0.0},
    "INVESTOR_B": {"name": "Investor B (SE)", "strategy": "trend",     "leverage": 5.0, "budget_sek": 2000.0, "product": "MINI L INVESTOR NORDNET SE23",   "yahoo": "INVE-B.ST", "avanza_id": "2286747",  "commission_sek": 0.0},
}

MARKET_OPEN_PKT  = (12, 0)   # 09:00 CET = 12:00 PKT
MARKET_CLOSE_PKT = (20, 30)  # 17:30 CET = 20:30 PKT

W = 78  # display width

# ── ANSI colours ───────────────────────────────────────────────────────────────

class C:
    RST    = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    # foregrounds
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    MAGENTA= "\033[95m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    GRAY   = "\033[90m"
    # compound
    BRED   = BOLD + RED
    BGRN   = BOLD + GREEN
    BYEL   = BOLD + YELLOW
    BCYN   = BOLD + CYAN
    BWHT   = BOLD + WHITE

def _c(text: str, *codes: str) -> str:
    return "".join(codes) + str(text) + C.RST

def _pnl_str(val: float, unit: str = "SEK") -> str:
    if val > 0:
        return _c(f"+{val:,.0f} {unit}", C.BGRN)
    if val < 0:
        return _c(f"{val:,.0f} {unit}", C.BRED)
    return _c(f"0 {unit}", C.GRAY)

def _pnl_pct_str(val_sek: float, pct: float) -> str:
    sign = "+" if val_sek >= 0 else ""
    text = f"{sign}{val_sek:,.0f} kr ({sign}{pct:.1f}%)"
    if val_sek > 0:   return _c(text, C.BGRN)
    if val_sek < 0:   return _c(text, C.BRED)
    return _c(text, C.GRAY)

def _pnl_color(val: float) -> str:
    if val > 0:  return f"+{val:.2f}"
    if val < 0:  return f"{val:.2f}"
    return "0.00"

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

def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "-"
    try:
        dt  = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        pkt = dt.astimezone(timezone.utc) + timedelta(hours=5)
        return pkt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16] if ts else "-"

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


_avanza_client_cache: list = []  # holds [client] once loaded


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

    # ── HEADER ────────────────────────────────────────────────────────────────
    print(_c("=" * W, C.CYAN))
    print(_c(f"{'AVANZA MINI FUTURES DASHBOARD':^{W}}", C.BCYN + C.BOLD))
    print(_c(f"  {now.strftime('%Y-%m-%d %H:%M')} PKT  |  Market: ", C.GRAY)
          + mkt_str
          + _c("  |  6 instruments", C.GRAY))
    print(_c("=" * W, C.CYAN))

    # ── PAPER TRADING (SIM) ───────────────────────────────────────────────────
    print()
    print(_c(f"  ┌{'─' * (W - 4)}┐", C.YELLOW))
    print(_c(f"  │{'  PAPER TRADING  (SIM)':^{W-4}}│", C.BYEL))
    print(_c(f"  └{'─' * (W - 4)}┘", C.YELLOW))
    print()

    hdr_open = (f"  {_c('Instrument', C.BWHT):<24} "
                f"{_c('Strat', C.BWHT):<14} "
                f"{_c('Lev', C.BWHT):>9}  "
                f"{_c('Entry', C.BWHT):>12}  "
                f"{_c('Now', C.BWHT):>12}  "
                f"{_c('Date', C.BWHT):<14}  "
                f"{_c('Open P&L', C.BWHT):>16}")
    print(hdr_open)
    print(_c(f"  {'-' * (W + 4)}", C.DIM))

    total_open_pnl   = 0.0
    total_closed_pnl = 0.0
    total_wins       = 0
    total_losses     = 0

    for key, cfg in INSTRUMENTS.items():
        p        = pos.get(key, {})
        in_pos   = p.get("status") == "OPEN" and bool(p.get("entry_price"))
        entry_px = p.get("entry_price", 0.0)
        entry_dt = p.get("entry_date", "")
        date_str = _c(entry_dt[:10], C.BLUE) if in_pos and entry_dt else _c("-", C.GRAY)

        strat_color = C.MAGENTA if cfg["strategy"] == "reversion" else C.CYAN
        name_str  = _c(f"{cfg['name']:<16}", C.WHITE)
        strat_str = _c(f"{cfg['strategy']:<10}", strat_color)
        lev_str   = _c(f"{cfg['leverage']:.1f}x", C.YELLOW)

        open_pnl_str = _c("-", C.GRAY)
        now_px_str   = _c("-", C.GRAY)
        entry_str    = _c("-", C.GRAY)

        if in_pos:
            entry_str = _c(f"{entry_px:>10.2f}", C.WHITE)
            cur = _fetch_price(cfg["yahoo"])
            if cur is not None:
                now_px_str = _c(f"{cur:>10.2f}", C.WHITE)
                pnl_sek, pnl_pct, is_ko = _current_pnl(p, cur)
                total_open_pnl += pnl_sek
                sign = "+" if pnl_sek >= 0 else ""
                if is_ko:
                    open_pnl_str = _c(f"KO! -{cfg['budget_sek']:.0f} SEK", C.BRED)
                else:
                    open_pnl_str = _pnl_str(pnl_sek) + _c(f" ({sign}{pnl_pct*100:.1f}%)", C.DIM)
            else:
                open_pnl_str = _c("no data", C.GRAY)

        print(f"  {name_str}  {strat_str}  {lev_str}  {entry_str}  {now_px_str}  {date_str}  {open_pnl_str}")

    # ── WARRANTER VIEW ────────────────────────────────────────────────────────
    open_positions = {k: v for k, v in pos.items()
                      if v.get("status") == "OPEN" and v.get("entry_sek")}
    if open_positions:
        print()
        print(_c(f"  {'AVANZA WARRANTER VIEW':^{W}}", C.BYEL))
        print(_c(f"  {'-' * (W - 2)}", C.YELLOW))
        print(f"  {_c('Namn', C.BWHT):<35} "
              f"{_c('Antal', C.BWHT):>5}  "
              f"{_c('Senast', C.BWHT):>8}  "
              f"{_c('Inkopskurs', C.BWHT):>10}  "
              f"{_c('Sedan kop', C.BWHT):>22}  "
              f"{_c('Varde', C.BWHT):>9}  "
              f"{_c('Comm', C.BWHT):>5}")
        print(_c(f"  {'-' * (W + 14)}", C.DIM))

        for key, p in open_positions.items():
            cfg       = INSTRUMENTS.get(key, {})
            namn      = cfg.get("product", key)[:28]
            qty       = p.get("qty", 0)
            entry_sek = p.get("entry_sek", 0.0)
            comm_sek  = cfg.get("commission_sek", 0.0)
            avanza_id = cfg.get("avanza_id", "")
            senast    = _fetch_avanza_sek(avanza_id) if avanza_id else None

            namn_str   = _c(f"{namn:<28}", C.CYAN)
            entry_str  = _c(f"{entry_sek:>10.2f}", C.WHITE)
            comm_str   = _c(f"{comm_sek:.0f} kr", C.GRAY)

            if senast and senast > 0:
                sedan_sek = (senast - entry_sek) * qty
                sedan_pct = (senast - entry_sek) / entry_sek * 100 if entry_sek else 0.0
                varde     = senast * qty
                senast_str = _c(f"{senast:>8.2f}", C.WHITE)
                sedan_str  = _pnl_pct_str(sedan_sek, sedan_pct)
                varde_col  = C.BGRN if sedan_sek >= 0 else C.BRED
                varde_str  = _c(f"{varde:,.0f} kr", varde_col)
            else:
                senast_str = _c("N/A", C.GRAY)
                sedan_str  = _c("N/A", C.GRAY)
                varde_str  = _c("N/A", C.GRAY)

            print(f"  {namn_str} {qty:>5}  {senast_str}  {entry_str}  {sedan_str}  {varde_str}  {comm_str}")

        print(_c("  AVA-branded: 0 kr brokerage (financing cost is implicit)", C.GRAY + C.DIM))

    # ── CLOSED TRADES ─────────────────────────────────────────────────────────
    print()
    print(_c(f"  {'Instrument':<16} {'Trades':>6} {'Wins':>5} {'Losses':>7} {'WR':>6} {'PF':>6} {'Closed P&L':>13} {'Comm':>5}  {'Gate':>12}", C.BWHT))
    print(_c(f"  {'-' * (W + 6)}", C.DIM))

    N_GATE = 5
    total_gross_win  = 0.0
    total_gross_loss = 0.0
    total_comm       = 0.0

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
        gate_str  = _c("READY", C.BGRN) if not remaining else _c(f"{remaining} more", C.YELLOW)

        total_closed_pnl += pnl
        total_wins       += wins
        total_losses     += losses
        total_gross_win  += gw
        total_gross_loss += gl
        total_comm       += comm

        wr_col  = C.BGRN if wr >= 50 else C.YELLOW if wr >= 35 else C.RED
        pf_col  = C.BGRN if pf >= 1.5 else C.YELLOW if pf >= 1.0 else C.RED
        name_c  = _c(f"{cfg['name']:<16}", C.WHITE)
        wr_c    = _c(f"{wr:>5.0f}%", wr_col)
        pf_c    = _c(f"{pf_str:>6}", pf_col)
        pnl_c   = _pnl_str(pnl)
        comm_c  = _c(f"{comm:.0f} kr", C.GRAY)
        print(f"  {name_c} {n:>6} {wins:>5} {losses:>7}  {wr_c}  {pf_c}  {pnl_c}  {comm_c}  {gate_str}")

    total_n   = total_wins + total_losses
    total_wr  = (total_wins / total_n * 100) if total_n else 0.0
    total_pf  = (total_gross_win / total_gross_loss) if total_gross_loss > 0 else (float("inf") if total_gross_win > 0 else 0.0)
    tpf_str   = f"{total_pf:.2f}" if total_pf != float("inf") else "inf"
    print(_c(f"  {'-' * (W + 6)}", C.DIM))
    print(f"  {_c('TOTAL', C.BWHT):<25} "
          f"{_c(str(total_n), C.WHITE):>6} "
          f"{_c(str(total_wins), C.GREEN):>5} "
          f"{_c(str(total_losses), C.RED):>7}  "
          f"{_c(f'{total_wr:.0f}%', C.WHITE):>9}  "
          f"{_c(tpf_str, C.WHITE):>9}  "
          f"{_pnl_str(total_closed_pnl)}  "
          f"{_c(f'{total_comm:.0f} kr', C.GRAY)}")

    if total_open_pnl != 0.0:
        sign = "+" if total_open_pnl >= 0 else ""
        col  = C.BGRN if total_open_pnl > 0 else C.BRED
        print(f"  {_c('Open P&L (live):', C.GRAY)}  {_c(f'{sign}{total_open_pnl:,.0f} SEK', col)}")

    # ── LIVE SEPARATOR ────────────────────────────────────────────────────────
    print()
    print(_c(f"  {'─' * (W - 4)}", C.BLUE + C.DIM))
    print(_c(f"  {'· · ·  AVANZA LIVE  · · ·':^{W-4}}", C.BLUE + C.BOLD))
    print(_c(f"  {'─' * (W - 4)}", C.BLUE + C.DIM))
    print()
    print(_c("  No live positions yet.", C.GRAY))
    print(_c("  Live trading unlocks when each instrument has 5 closed paper trades with positive P&L.", C.GRAY))
    print()
    print(_c("  Gate status:", C.BWHT))
    for key, cfg in INSTRUMENTS.items():
        inst_trades = [t for t in trades if t.get("instrument") == key]
        n    = len(inst_trades)
        pnl  = sum(t.get("pnl_sek", t.get("pnl", 0)) for t in inst_trades)
        done = n >= N_GATE and pnl > 0
        filled = "#" * n
        empty  = "." * max(0, N_GATE - n)
        bar    = _c(filled, C.BGRN) + _c(empty, C.GRAY)
        flag   = _c("READY", C.BGRN) if done else _c(f"{max(0, N_GATE - n)} left", C.YELLOW)
        pnl_d  = _pnl_str(pnl)
        nm = f"{cfg['name']:<16}"
        print(f"    {_c(nm, C.WHITE)} [{bar}]  {n}/{N_GATE}   {pnl_d}   {flag}")

    # ── RECENT ACTIVITY ───────────────────────────────────────────────────────
    print()
    print(_c(f"  {'─' * (W - 4)}", C.DIM))
    print(_c("  Recent activity:", C.BWHT))
    for line in _last_log_lines(5):
        print(_c(f"    {line[:W - 6]}", C.GRAY))

    print()
    print(_c(f"  Scheduler: 13:30 / 19:30 / 20:00 PKT  |  Market hours: 12:00-20:30 PKT Mon-Fri", C.GRAY))
    print(_c(f"  State: {_STATE_FILE}", C.GRAY + C.DIM))
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
