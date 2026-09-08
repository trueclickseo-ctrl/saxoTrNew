"""
avanza_paper_trading.py
-----------------------
Paper trading dry-run for Avanza mini futures (DAX + S&P 500 reversion).

Run this daily (or any time). It:
  1. Fetches recent prices from Yahoo Finance
  2. Calculates the 20-day MA for each instrument
  3. Detects entry/exit signals
  4. Auto-enters / auto-exits paper positions — no real money touched
  5. Prints a status report with open P&L and trade history

After N_MIN_TRADES paper trades per instrument with positive expectancy,
review and decide whether to go live on Avanza.

Usage:
    python avanza_paper_trading.py              # check signals + update
    python avanza_paper_trading.py --status     # status only, no update
    python avanza_paper_trading.py --history    # full trade history
    python avanza_paper_trading.py --reset      # clear all paper state
    python avanza_paper_trading.py --add-gold   # also track Gold trend signal
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

_ROOT       = os.path.dirname(os.path.abspath(__file__))
_STATE_FILE = os.path.join(_ROOT, "data", "avanza_paper_positions.json")

# ── Instrument config ─────────────────────────────────────────────────────────

INSTRUMENTS = {
    "DAX": {
        "yahoo":       "^GDAXI",
        "name":        "DAX (Germany)",
        "strategy":    "reversion",
        "leverage":    5.2,
        "budget_sek":  2000.0,
        "financing_rate": 0.055,
        "ma_days":     20,
        "active":      True,
        # True mini future on Nordic MTF: parity 1000 (1000 units = 1 DAX point)
        # barrier=21003, financing=20591, DAX~26007 → KO distance 19.2% → 5.2x actual leverage
        "avanza_id":   "2047459",
        "avanza_name": "MINI L DAX LEV18",
        "parity":      1000,
    },
    "SP500": {
        "yahoo":       "^GSPC",
        "name":        "S&P 500 (US)",
        "strategy":    "reversion",
        "leverage":    4.9,
        "budget_sek":  2000.0,
        "financing_rate": 0.055,
        "ma_days":     20,
        "active":      True,
        # True mini future on Nordic MTF: parity 100 (100 units = 1 SPX point)
        # barrier=5904, financing=5788, SPX~7400 → KO distance 20.2% → 4.9x actual leverage
        "avanza_id":   "2129934",
        "avanza_name": "MINI L SPX LEV12",
        "parity":      100,
    },
    "GOLD": {
        "yahoo":       "GC=F",
        "name":        "Gold",
        "strategy":    "trend",
        "leverage":    5,
        "budget_sek":  2000.0,
        "financing_rate": 0.055,
        "ma_days":     20,
        "active":      False,   # enabled via --add-gold
        # Certificate (BULL GULD X5 N) — true mini future ID TBD when Gold added
        "avanza_id":   "856393",
        "avanza_name": "BULL GULD X5 N",
        "parity":      None,
    },
}

N_MIN_TRADES = 5    # minimum paper trades before recommending live

# ── Data ─────────────────────────────────────────────────────────────────────

def _download(yahoo: str, days: int = 60) -> tuple[list[float], list[str]]:
    """Return (closes, dates) for the last `days` calendar days."""
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("  ERROR: yfinance not installed — pip install yfinance")

    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    df = yf.download(yahoo,
                     start=start.strftime("%Y-%m-%d"),
                     end=end.strftime("%Y-%m-%d"),
                     auto_adjust=True, progress=False)
    if df.empty:
        return [], []

    closes = df["Close"]
    if hasattr(closes, "squeeze"):
        closes = closes.squeeze()
    dates  = [str(d.date()) for d in df.index.tolist()]
    return [float(x) for x in closes.tolist()], dates


def _ma(closes: list[float], n: int) -> Optional[float]:
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


# ── Signal detection ──────────────────────────────────────────────────────────

def _detect_signal(closes: list[float], dates: list[str],
                   strategy: str, ma_days: int
                   ) -> tuple[str, float, float, str]:
    """
    Returns (signal, current_price, ma_value, date).
    signal: 'ENTRY' | 'EXIT' | 'HOLD_IN' | 'HOLD_OUT' | 'NO_DATA'
    """
    if len(closes) < ma_days + 1:
        return "NO_DATA", 0.0, 0.0, ""

    price_now  = closes[-1]
    price_prev = closes[-2]
    ma_now     = _ma(closes, ma_days)
    ma_prev    = _ma(closes[:-1], ma_days)
    date_now   = dates[-1]

    if ma_now is None or ma_prev is None:
        return "NO_DATA", price_now, 0.0, date_now

    if strategy == "reversion":
        crossed_below = price_prev >= ma_prev and price_now < ma_now
        crossed_above = price_prev <= ma_prev and price_now > ma_now
        below_ma      = price_now < ma_now

        if crossed_below:
            return "ENTRY", price_now, ma_now, date_now
        if crossed_above:
            return "EXIT", price_now, ma_now, date_now
        if below_ma:
            return "HOLD_IN", price_now, ma_now, date_now
        return "HOLD_OUT", price_now, ma_now, date_now

    else:  # trend
        crossed_above = price_prev <= ma_prev and price_now > ma_now
        crossed_below = price_prev >= ma_prev and price_now < ma_now
        above_ma      = price_now > ma_now

        if crossed_above:
            return "ENTRY", price_now, ma_now, date_now
        if crossed_below:
            return "EXIT", price_now, ma_now, date_now
        if above_ma:
            return "HOLD_IN", price_now, ma_now, date_now
        return "HOLD_OUT", price_now, ma_now, date_now


# ── State ─────────────────────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(_STATE_FILE):
        try:
            with open(_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"positions": {}, "trades": []}


def _save(state: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    tmp = _STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, _STATE_FILE)


# ── Position P&L ─────────────────────────────────────────────────────────────

def _current_pnl(pos: dict, current_price: float) -> tuple[float, float, bool]:
    """Returns (pnl_sek, pnl_pct, is_ko)."""
    entry_price     = pos["entry_price"]
    financing_entry = pos["financing_entry"]
    budget          = pos["budget_sek"]
    lever           = pos["leverage"]
    rate            = pos.get("financing_rate", 0.055)

    # Days elapsed (approximate)
    try:
        entry_dt = datetime.fromisoformat(pos["entry_date"])
        elapsed  = (datetime.now() - entry_dt).days
    except Exception:
        elapsed = 0

    # Current financing level (drifted)
    daily_rate = rate / 252
    financing_now = financing_entry * ((1 + daily_rate) ** elapsed)

    # Knock-out check
    is_ko = current_price <= financing_now

    if is_ko:
        return -budget, -1.0, True

    pos_return = (current_price - financing_now) / (entry_price - financing_entry) - 1.0
    pnl_sek    = round(budget * pos_return, 2)
    return pnl_sek, pos_return, False


# ── Core update ───────────────────────────────────────────────────────────────

def run_update(state: dict, dry_run: bool = False) -> None:
    print(f"\n  {'='*62}")
    print(f"  AVANZA PAPER TRADING  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  {'='*62}")

    for key, cfg in INSTRUMENTS.items():
        if not cfg["active"]:
            continue

        print(f"\n  [{key}]  {cfg['name']}  ({cfg['strategy'].upper()}  {cfg['leverage']}x)")
        closes, dates = _download(cfg["yahoo"], days=60)

        if not closes:
            print(f"    No price data available.")
            continue

        signal, price, ma_val, sig_date = _detect_signal(
            closes, dates, cfg["strategy"], cfg["ma_days"]
        )

        pos       = state["positions"].get(key)
        in_market = pos is not None and pos.get("status") == "OPEN"

        # ── Current price & MA ────────────────────────────────────────────
        diff_pct = (price - ma_val) / ma_val * 100 if ma_val else 0
        above    = "above" if price > ma_val else "below"
        print(f"    Price : {price:,.2f}  |  20-day MA: {ma_val:,.2f}  "
              f"({diff_pct:+.2f}% {above} MA)")

        # ── Signal ───────────────────────────────────────────────────────
        signal_labels = {
            "ENTRY":    "*** ENTRY SIGNAL — price just crossed below MA ***",
            "EXIT":     "*** EXIT SIGNAL  — price just crossed above MA ***",
            "HOLD_IN":  "Signal: HOLD (below MA — in-signal zone)",
            "HOLD_OUT": "Signal: FLAT (above MA — no trade)",
            "NO_DATA":  "Signal: NO DATA",
        }
        print(f"    {signal_labels.get(signal, signal)}")

        # ── Entry ─────────────────────────────────────────────────────────
        if signal == "ENTRY" and not in_market:
            lever    = cfg["leverage"]
            fin_lvl  = price * (1.0 - 1.0 / lever)
            ko_dist  = (price - fin_lvl) / price * 100

            avanza_name = cfg.get("avanza_name", "")
            avanza_id   = cfg.get("avanza_id", "")
            print(f"\n    >> PAPER ENTRY at {price:,.2f}")
            print(f"       Avanza instrument: {avanza_name}  (id={avanza_id})")
            print(f"       Financing level (KO barrier): {fin_lvl:,.2f}  "
                  f"({ko_dist:.1f}% below current price)")
            print(f"       Budget: {cfg['budget_sek']:,.0f} SEK  |  Leverage: {lever}x")

            if not dry_run:
                state["positions"][key] = {
                    "status":          "OPEN",
                    "entry_date":      sig_date,
                    "entry_price":     price,
                    "financing_entry": fin_lvl,
                    "budget_sek":      cfg["budget_sek"],
                    "leverage":        lever,
                    "financing_rate":  cfg["financing_rate"],
                    "strategy":        cfg["strategy"],
                    "ma_at_entry":     ma_val,
                }
                print(f"       Paper position recorded.")
            else:
                print(f"       [dry-run] not recorded.")

        # ── Exit ──────────────────────────────────────────────────────────
        elif signal == "EXIT" and in_market:
            pnl_sek, pnl_pct, is_ko = _current_pnl(pos, price)
            sign = "+" if pnl_sek >= 0 else ""

            print(f"\n    >> PAPER EXIT at {price:,.2f}")
            print(f"       Entry: {pos['entry_price']:,.2f}  on {pos['entry_date']}")
            print(f"       P&L  : {sign}{pnl_sek:,.2f} SEK  ({sign}{pnl_pct*100:.1f}%)")

            if not dry_run:
                trade = {
                    "instrument":  key,
                    "entry_date":  pos["entry_date"],
                    "exit_date":   sig_date,
                    "entry_price": pos["entry_price"],
                    "exit_price":  price,
                    "pnl_sek":     pnl_sek,
                    "pnl_pct":     round(pnl_pct * 100, 2),
                    "leverage":    pos["leverage"],
                    "ko":          is_ko,
                    "strategy":    pos["strategy"],
                }
                state["trades"].append(trade)
                state["positions"][key] = {"status": "FLAT"}
                print(f"       Paper position closed and recorded.")

        # ── Open position status ──────────────────────────────────────────
        elif in_market:
            pnl_sek, pnl_pct, is_ko = _current_pnl(pos, price)
            sign = "+" if pnl_sek >= 0 else ""
            ko_pct = (price - pos["financing_entry"]) / price * 100
            print(f"\n    >> POSITION OPEN  (entered {pos['entry_date']} @ {pos['entry_price']:,.2f})")
            print(f"       Live P&L : {sign}{pnl_sek:,.2f} SEK  ({sign}{pnl_pct*100:.1f}%)")
            print(f"       KO level : {pos['financing_entry']:,.2f}  "
                  f"({ko_pct:.1f}% cushion — {'SAFE' if ko_pct > 10 else 'WATCH'})")
            if is_ko:
                print(f"       *** KNOCK-OUT — position would be wiped ***")

        else:
            print(f"    No open position. Waiting for entry signal.")


# ── Status report ─────────────────────────────────────────────────────────────

def print_status(state: dict) -> None:
    trades = state.get("trades", [])
    print(f"\n  {'='*62}")
    print(f"  PAPER TRADING STATUS")
    print(f"  {'='*62}")

    for key, cfg in INSTRUMENTS.items():
        if not cfg["active"]:
            continue
        pos = state["positions"].get(key, {})
        t   = [x for x in trades if x["instrument"] == key]
        wins = [x for x in t if x["pnl_sek"] > 0]
        losses = [x for x in t if x["pnl_sek"] <= 0]
        total_pnl = sum(x["pnl_sek"] for x in t)
        wr   = len(wins) / len(t) * 100 if t else 0

        print(f"\n  [{key}]  {cfg['name']}")
        print(f"    Closed trades : {len(t)}  "
              f"(wins {len(wins)}, losses {len(losses)}, WR {wr:.0f}%)")
        print(f"    Total P&L     : {total_pnl:+,.2f} SEK")

        if len(t) >= N_MIN_TRADES and total_pnl > 0:
            print(f"    *** {len(t)} trades done, positive — CONSIDER GOING LIVE ***")
        elif len(t) < N_MIN_TRADES:
            remaining = N_MIN_TRADES - len(t)
            print(f"    Need {remaining} more paper trade(s) before live review.")

        status = pos.get("status", "FLAT")
        print(f"    Current position: {status}")


def print_history(state: dict) -> None:
    trades = state.get("trades", [])
    if not trades:
        print("\n  No closed paper trades yet.")
        return

    print(f"\n  {'='*70}")
    print(f"  PAPER TRADE HISTORY ({len(trades)} trades)")
    print(f"  {'='*70}")
    print(f"  {'#':>3}  {'Instr':6s}  {'Entry':10s}  {'Exit':10s}  "
          f"{'Entry P':>10}  {'Exit P':>10}  {'P&L SEK':>9}  Note")
    print(f"  {'-'*70}")

    equity = 0.0
    for i, t in enumerate(trades, 1):
        ko = " KO!" if t.get("ko") else ""
        sign = "+" if t["pnl_sek"] >= 0 else ""
        print(f"  {i:>3}  {t['instrument']:6s}  {t['entry_date']}  {t['exit_date']}  "
              f"{t['entry_price']:>10,.2f}  {t['exit_price']:>10,.2f}  "
              f"{sign}{t['pnl_sek']:>8,.2f}{ko}")
        equity += t["pnl_sek"]

    print(f"\n  Total paper P&L: {equity:+,.2f} SEK across all instruments")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="Avanza mini futures paper trading — DAX + S&P 500 reversion signal"
    )
    p.add_argument("--status",    action="store_true", help="Show status only, no update")
    p.add_argument("--history",   action="store_true", help="Show full trade history")
    p.add_argument("--reset",     action="store_true", help="Clear all paper positions")
    p.add_argument("--add-gold",  action="store_true", help="Also track Gold trend signal")
    p.add_argument("--dry-run",   action="store_true", help="Detect signals but don't record")
    args = p.parse_args()

    if args.add_gold:
        INSTRUMENTS["GOLD"]["active"] = True

    state = _load()

    if args.reset:
        confirm = input("  Reset all paper positions and trade history? [y/n]: ").strip().lower()
        if confirm == "y":
            state = {"positions": {}, "trades": []}
            _save(state)
            print("  Paper state cleared.")
        return

    if args.history:
        print_history(state)
        return

    if args.status:
        print_status(state)
        return

    # Default: run signal check + update
    run_update(state, dry_run=args.dry_run)

    if not args.dry_run:
        _save(state)

    print_status(state)
    if state.get("trades"):
        print_history(state)


if __name__ == "__main__":
    main()
