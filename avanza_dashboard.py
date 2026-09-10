"""
avanza_dashboard.py  —  Avanza mini futures dashboard
------------------------------------------------------
Shows Paper Trading (SIM) and Live sections with a separator.
Live section is a placeholder until real money moves to Avanza.

Usage:
    python avanza_dashboard.py          # refresh every 30s
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

REFRESH_SECONDS = 30

INSTRUMENTS = {
    "DAX":        {"name": "DAX (Germany)",   "strategy": "reversion", "leverage": 5.4, "budget_sek": 2000.0, "product": "MINI L DAX AVA 850",            "yahoo": "^GDAXI"},
    "SP500":      {"name": "S&P 500 (US)",    "strategy": "reversion", "leverage": 5.8, "budget_sek": 2000.0, "product": "MINI L SP500 AVA 339",           "yahoo": "^GSPC"},
    "GOLD":       {"name": "Gold",            "strategy": "trend",     "leverage": 5.0, "budget_sek": 2000.0, "product": "MINI L GULD AVA 247",            "yahoo": "GC=F"},
    "APPLE":      {"name": "Apple (AAPL)",    "strategy": "trend",     "leverage": 5.2, "budget_sek": 2000.0, "product": "MINI L APPLE AVA 91",            "yahoo": "AAPL"},
    "GOOGLE":     {"name": "Google (GOOGL)",  "strategy": "trend",     "leverage": 4.7, "budget_sek": 2000.0, "product": "MINI L GOOGLE AVA 63",           "yahoo": "GOOGL"},
    "INVESTOR_B": {"name": "Investor B (SE)", "strategy": "trend",     "leverage": 5.0, "budget_sek": 2000.0, "product": "MINI L INVESTOR NORDNET SE23",   "yahoo": "INVE-B.ST"},
}

MARKET_OPEN_PKT  = (12, 0)   # 09:00 CET = 12:00 PKT
MARKET_CLOSE_PKT = (20, 30)  # 17:30 CET = 20:30 PKT

W = 72  # display width

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
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(hours=5)

def _market_open() -> bool:
    now = _now_pkt()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    oh, om = MARKET_OPEN_PKT
    ch, cm = MARKET_CLOSE_PKT
    return (h * 60 + m) >= (oh * 60 + om) and (h * 60 + m) < (ch * 60 + cm)

def _pnl_color(val: float) -> str:
    if val > 0:  return f"+{val:.2f}"
    if val < 0:  return f"{val:.2f}"
    return "0.00"

def _last_log_lines(n: int = 5) -> list[str]:
    try:
        with open(_LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip() for l in lines[-n:]]
    except Exception:
        return []

def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        pkt = dt.astimezone(timezone.utc)
        from datetime import timedelta
        pkt = pkt + timedelta(hours=5)
        return pkt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ts[:16] if ts else "—"

# ── live prices ────────────────────────────────────────────────────────────────

_price_cache: dict[str, tuple[float, datetime]] = {}
_CACHE_TTL_S = 60

def _fetch_price(yahoo: str) -> float | None:
    """Return latest close/price from Yahoo Finance, cached 60s."""
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


def _current_pnl(pos: dict, current_price: float) -> tuple[float, float, bool]:
    """Returns (pnl_sek, pnl_pct, is_ko) — mirrors avanza_paper_trading._current_pnl."""
    entry_price     = pos["entry_price"]
    financing_entry = pos["financing_entry"]
    budget          = pos["budget_sek"]
    rate            = pos.get("financing_rate", 0.055)
    try:
        entry_dt  = datetime.fromisoformat(str(pos["entry_date"]))
        elapsed   = (datetime.now() - entry_dt).days
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
    mkt    = "OPEN" if _market_open() else "CLOSED"

    print("=" * W)
    print(f"  AVANZA MINI FUTURES DASHBOARD".center(W))
    print(f"  {now.strftime('%Y-%m-%d %H:%M')} PKT  |  Avanza market: {mkt}  |  6 instruments".center(W))
    print("=" * W)

    # ── SECTION 1: PAPER TRADING (SIM) ────────────────────────────────────────
    print()
    print(f"  ┌{'─' * (W - 4)}┐")
    print(f"  │{'  PAPER TRADING  (SIM)':^{W-4}}│")
    print(f"  └{'─' * (W - 4)}┘")
    print()

    # header
    print(f"  {'Instrument':<14} {'Strategy':<10} {'Lev':>5}  {'Position':<10} {'Entry':>8}  {'Now':>8}  {'Entry Date':<12} {'Budget':>8}  {'Open P&L':>12}")
    print(f"  {'-' * (W + 6)}")

    total_open_pnl   = 0.0
    total_closed_pnl = 0.0
    total_wins       = 0
    total_losses     = 0

    for key, cfg in INSTRUMENTS.items():
        p = pos.get(key, {})
        in_pos   = bool(p.get("entry_price"))
        entry_px = p.get("entry_price", 0.0)
        entry_dt = _fmt_ts(p.get("entry_date"))
        direction = p.get("direction", "LONG") if in_pos else "-"

        open_pnl_str = "-"
        now_px_str   = "-"
        if in_pos and entry_px:
            cur = _fetch_price(cfg["yahoo"])
            if cur is not None:
                now_px_str = f"{cur:>8.2f}"
                pnl_sek, pnl_pct, is_ko = _current_pnl(p, cur)
                total_open_pnl += pnl_sek
                sign = "+" if pnl_sek >= 0 else ""
                ko_tag = " KO!" if is_ko else ""
                open_pnl_str = f"{sign}{pnl_sek:,.0f} SEK ({sign}{pnl_pct*100:.1f}%){ko_tag}"
            else:
                open_pnl_str = "no data"

        pos_str   = f"{direction}" if in_pos else "FLAT"
        entry_str = f"{entry_px:>8.2f}" if in_pos else f"{'':>8}"
        date_str  = entry_dt[:10] if in_pos else "-"
        print(f"  {cfg['name']:<14} {cfg['strategy']:<10} {cfg['leverage']:>4.1f}x  {pos_str:<10} {entry_str}  {now_px_str:>8}  {date_str:<12} {cfg['budget_sek']:>7.0f}s  {open_pnl_str:>12}")

    # closed trades per instrument
    print()
    print(f"  {'Instrument':<14} {'Trades':>7} {'Wins':>5} {'Losses':>7} {'WR':>6} {'Closed P&L':>12}  {'Gate':>18}")
    print(f"  {'-' * (W - 4)}")

    N_GATE = 5
    for key, cfg in INSTRUMENTS.items():
        inst_trades = [t for t in trades if t.get("instrument") == key]
        n      = len(inst_trades)
        wins   = sum(1 for t in inst_trades if t.get("pnl", 0) > 0)
        losses = n - wins
        wr     = (wins / n * 100) if n else 0.0
        pnl    = sum(t.get("pnl", 0) for t in inst_trades)
        remaining = max(0, N_GATE - n)
        gate_str  = f"{remaining} more to review" if remaining else "READY FOR LIVE REVIEW"
        total_closed_pnl += pnl
        total_wins       += wins
        total_losses     += losses

        pnl_str = _pnl_color(pnl) + " SEK"
        print(f"  {cfg['name']:<14} {n:>7} {wins:>5} {losses:>7} {wr:>5.0f}%  {pnl_str:>12}  {gate_str:>18}")

    total_n  = total_wins + total_losses
    total_wr = (total_wins / total_n * 100) if total_n else 0.0
    print(f"  {'-' * (W - 4)}")
    print(f"  {'TOTAL':<14} {total_n:>7} {total_wins:>5} {total_losses:>7} {total_wr:>5.0f}%  {_pnl_color(total_closed_pnl) + ' SEK':>12}")
    if total_open_pnl != 0.0:
        sign = "+" if total_open_pnl >= 0 else ""
        print(f"  Open P&L (live) : {sign}{total_open_pnl:,.0f} SEK")

    # ── SEPARATOR ─────────────────────────────────────────────────────────────
    print()
    print(f"  {'─' * (W - 4)}")
    print(f"  {'· · ·  AVANZA LIVE  · · ·':^{W-4}}")
    print(f"  {'─' * (W - 4)}")
    print()
    print(f"  No live positions yet.")
    print(f"  Live trading unlocks when each instrument has 5 closed paper trades with positive P&L.")
    print()
    print(f"  Gate status:")
    for key, cfg in INSTRUMENTS.items():
        inst_trades = [t for t in trades if t.get("instrument") == key]
        n    = len(inst_trades)
        pnl  = sum(t.get("pnl", 0) for t in inst_trades)
        done = n >= N_GATE and pnl > 0
        bar  = "#" * n + "." * max(0, N_GATE - n)
        flag = "READY" if done else f"{max(0, N_GATE - n)} left"
        print(f"    {cfg['name']:<14} [{bar}]  {n}/{N_GATE} trades   {_pnl_color(pnl)+' SEK':>10}   {flag}")

    # ── recent log ────────────────────────────────────────────────────────────
    print()
    print(f"  {'─' * (W - 4)}")
    print(f"  Recent activity:")
    for line in _last_log_lines(5):
        print(f"    {line[:W - 6]}")

    print()
    print(f"  Scheduler: 13:30 / 19:30 / 20:00 PKT  |  Market hours: 12:00-20:30 PKT Mon-Fri")
    print(f"  State: {_STATE_FILE}")
    print("=" * W)


def main():
    once = "--once" in sys.argv
    while True:
        _clear()
        render()
        if once:
            break
        print(f"\n  Refreshing every {REFRESH_SECONDS}s  (Ctrl+C to exit)")
        try:
            time.sleep(REFRESH_SECONDS)
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
