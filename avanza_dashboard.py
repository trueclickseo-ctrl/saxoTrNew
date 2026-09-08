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
from datetime import datetime, timezone

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
    "DAX":    {"name": "DAX (Germany)",  "strategy": "reversion", "leverage": 5.4, "budget_sek": 2000.0, "product": "MINI L DAX AVA 850"},
    "SP500":  {"name": "S&P 500 (US)",   "strategy": "reversion", "leverage": 5.8, "budget_sek": 2000.0, "product": "MINI L SP500 AVA 339"},
    "GOLD":   {"name": "Gold",           "strategy": "trend",     "leverage": 5.0, "budget_sek": 2000.0, "product": "MINI L GULD AVA 247"},
    "APPLE":  {"name": "Apple (AAPL)",   "strategy": "trend",     "leverage": 5.2, "budget_sek": 2000.0, "product": "MINI L APPLE AVA 91"},
    "GOOGLE": {"name": "Google (GOOGL)", "strategy": "trend",     "leverage": 4.7, "budget_sek": 2000.0, "product": "MINI L GOOGLE AVA 63"},
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

# ── render ─────────────────────────────────────────────────────────────────────

def render():
    state  = _load_state()
    trades = state.get("trades", [])
    pos    = state.get("positions", {})
    now    = _now_pkt()
    mkt    = "OPEN" if _market_open() else "CLOSED"

    print("=" * W)
    print(f"  AVANZA MINI FUTURES DASHBOARD".center(W))
    print(f"  {now.strftime('%Y-%m-%d %H:%M')} PKT  |  Avanza market: {mkt}  |  5 instruments".center(W))
    print("=" * W)

    # ── SECTION 1: PAPER TRADING (SIM) ────────────────────────────────────────
    print()
    print(f"  ┌{'─' * (W - 4)}┐")
    print(f"  │{'  PAPER TRADING  (SIM)':^{W-4}}│")
    print(f"  └{'─' * (W - 4)}┘")
    print()

    # header
    print(f"  {'Instrument':<14} {'Strategy':<10} {'Lev':>5}  {'Position':<10} {'Entry':>8}  {'Entry Date':<17} {'Budget':>8}  {'Open P&L':>10}")
    print(f"  {'-' * (W - 4)}")

    total_open_pnl   = 0.0
    total_closed_pnl = 0.0
    total_wins       = 0
    total_losses     = 0

    for key, cfg in INSTRUMENTS.items():
        p = pos.get(key, {})
        in_pos   = bool(p.get("entry_price"))
        entry_px = p.get("entry_price", 0.0)
        entry_dt = _fmt_ts(p.get("entry_date"))
        direction = p.get("direction", "LONG") if in_pos else "—"

        # open P&L: paper tracking uses Yahoo close prices — we show "live ~" note
        open_pnl_str = "—"
        if in_pos and entry_px:
            open_pnl_str = "tracking..."

        pos_str = f"{direction}" if in_pos else "FLAT"

        entry_str = f"{entry_px:>8.2f}" if in_pos else f"{'':>8}"
        print(f"  {cfg['name']:<14} {cfg['strategy']:<10} {cfg['leverage']:>4.1f}x  {pos_str:<10} {entry_str}  {entry_dt if in_pos else '—':<17} {cfg['budget_sek']:>7.0f}s  {open_pnl_str:>10}")

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
