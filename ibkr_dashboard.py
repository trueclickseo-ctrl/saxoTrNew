"""
ibkr_dashboard.py
-----------------
Live dashboard for the IBKR paper stocks sleeve.

Displays open positions grouped by strategy with IBKR live prices
(falls back to Yahoo estimates when market is closed), entry price,
current gain%, stop level, and slot usage per strategy.

Usage:
    python ibkr_dashboard.py              # auto-refresh every 30s
    python ibkr_dashboard.py --once       # print once and exit
    python ibkr_dashboard.py --interval 60
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)


def _load_env() -> None:
    env_file = os.path.join(_ROOT, ".env.ibkr")
    if not os.path.exists(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _load_config() -> dict:
    cfg_path = os.path.join(_ROOT, "ibkr_module", "config", "ibkr_config.json")
    with open(cfg_path, encoding="utf-8") as f:
        return json.load(f)


# ── Strategy display order ────────────────────────────────────────────────────
# (db_key, label, config_key, slot_field, description)
# config_key supports nested paths with "." (e.g. "scorer.swing")
_STRATEGY_ROWS = [
    ("scorer_swing",     "Scorer Swing",      "scorer.swing",      "max_positions",
     "High-momentum breakout picks — top ROC/ADX/RS-vs-SPY from 482-stock universe"),
    ("scorer_portfolio", "Scorer Portfolio",  "scorer.portfolio",  "max_positions",
     "Quality growth holds — above SMA200, strong trend, steady fundamentals"),
    ("blend",            "US Blend",          "blend",             "max_positions",
     "Top-ranked US equities, weekly rebalance, equal-weight"),
    ("reversion",        "US Reversion",      "reversion",         "max_slots",
     "Oversold dip-buys — RSI(2) oversold, mean-reversion exit"),
    ("intraday",         "US Intraday Rev.",  "reversion",         "max_slots",
     "Intraday oversold bounces — same-day exit before close"),
    ("US SMA Crossover", "US SMA Crossover",  "signals",           "max_per_strategy",
     "50/200 SMA golden-cross momentum entries"),
    ("US RSI Reversal",  "US RSI Reversal",   "signals",           "max_per_strategy",
     "RSI oversold reversal with volume confirmation"),
    ("US Momentum",      "US Momentum",       "signals",           "max_per_strategy",
     "52-week high breakout, relative strength leaders"),
    ("US Ensemble",      "US Ensemble",       "signals",           "max_per_strategy",
     "Combined signal — requires SMA + RSI + momentum alignment"),
]

_W = 86  # terminal width

_GRN  = "\033[92m"
_RED  = "\033[91m"
_YLW  = "\033[93m"
_DIM  = "\033[2m"
_BOLD = "\033[1m"
_RST  = "\033[0m"

_COL_HDR = (
    f"  {'#':>2}  {'Symbol':<6}  {'Qty':>5}  "
    f"{'Entry':>9}  {'Last':>9}  {'Gain %':>7}  {'Gain $':>8}  "
    f"{'Stop':>9}  {'Value':>9}  {'Days':>4}"
)
_COL_SEP = "  " + "─" * (_W - 2)


def _max_slots(cfg: dict, cfg_key: str, slot_field: str) -> int:
    node = cfg.get("strategies", {})
    for part in cfg_key.split("."):
        node = node.get(part, {})
    return int(node.get(slot_field, 5) if isinstance(node, dict) else 5)


def _days_held(filled_at: str | None) -> int:
    if not filled_at:
        return 0
    try:
        dt = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return 0


def _pct(entry: float, last: float) -> float | None:
    if entry and entry > 0 and last and last > 0:
        return (last / entry - 1) * 100
    return None


def _color_pct(pct: float | None) -> str:
    if pct is None:
        return f"{'n/a':>7}"
    sign = "+" if pct >= 0 else ""
    s = f"{sign}{pct:.2f}%"
    color = _GRN if pct > 0 else (_RED if pct < 0 else "")
    return f"{color}{s:>7}{_RST}" if color else f"{s:>7}"


def _color_dollar(val: float, width: int = 8) -> str:
    if math.isnan(val):
        return f"{'n/a':>{width}}"
    sign = "+" if val > 0 else ""
    s = f"{sign}${val:,.0f}"
    color = _GRN if val > 0 else (_RED if val < 0 else "")
    return f"{color}{s:>{width}}{_RST}" if color else f"{s:>{width}}"


def _fmt_price(val: float) -> str:
    if not val or math.isnan(val):
        return f"{'—':>9}"
    return f"${val:>8,.2f}"


def render_dashboard(cfg: dict, summary: dict, account_id: str,
                     positions_by_strat: dict[str, list[dict]],
                     live_prices: dict[str, float],
                     yahoo_fallback: bool) -> None:
    os.system("cls" if os.name == "nt" else "clear")

    mode      = "PAPER" if cfg.get("paper", True) else "LIVE"
    now       = time.strftime("%Y-%m-%d %H:%M:%S")
    total_pos = sum(len(v) for v in positions_by_strat.values())
    price_lbl = "Yahoo (delayed)" if yahoo_fallback else "IBKR live"

    # ── Header ────────────────────────────────────────────────────────────────
    ccy = summary.get("currency", "USD")
    nl  = summary.get("net_liquidation", 0)
    cb  = summary.get("cash_balance", 0)
    u   = summary.get("unrealized_pnl", 0)
    r   = summary.get("realized_pnl", 0)

    print()
    print(f"  {_BOLD}IBKR {mode}  ·  {account_id} ({ccy})  ·  {now}  ·  {price_lbl}{_RST}")
    print("  " + "═" * _W)
    print(f"  Net Liq : {ccy} {nl:>12,.0f}"
          f"    Cash : {ccy} {cb:>12,.0f}"
          f"    Positions : {total_pos}")

    u_col = _GRN if u >= 0 else _RED
    r_col = _GRN if r >= 0 else _RED
    u_s   = f"{'+' if u>=0 else ''}{ccy} {u:,.0f}"
    r_s   = f"{'+' if r>=0 else ''}{ccy} {r:,.0f}"
    print(f"  Unreal  : {u_col}{u_s:>18}{_RST}"
          f"    Realized : {r_col}{r_s:>14}{_RST}")
    print("  " + "─" * _W)

    # ── Active strategy sections ───────────────────────────────────────────────
    idle_labels: list[str] = []

    for db_key, label, cfg_key, slot_field, desc in _STRATEGY_ROWS:
        positions = positions_by_strat.get(db_key, [])
        max_s     = _max_slots(cfg, cfg_key, slot_field)
        used      = len(positions)
        slot_bar  = f"{'█' * used}{'░' * (max_s - used)}"

        if not positions:
            idle_labels.append(f"{label} 0/{max_s}")
            continue

        # Section header + description
        print(f"\n  {_BOLD}── {label}{_RST}  [{slot_bar}] {used}/{max_s}")
        print(f"  {_DIM}   {desc}{_RST}")
        print(_COL_HDR)
        print(_COL_SEP)

        sect_invested = 0.0
        sect_pnl      = 0.0

        for idx, pos in enumerate(sorted(positions, key=lambda p: p.get("filled_at") or ""), 1):
            sym    = pos["symbol"]
            qty    = int(pos.get("qty", 0))
            entry  = float(pos.get("fill_price") or pos.get("limit_price") or 0)
            last   = float(live_prices.get(sym) or 0)
            stop   = float(pos.get("stop_price") or 0)
            days   = _days_held(pos.get("filled_at"))

            pct    = _pct(entry, last)
            gain_d = (last - entry) * qty if (entry and last) else float("nan")
            value  = last * qty if last else float("nan")

            if not math.isnan(gain_d):
                sect_pnl += gain_d
            if entry:
                sect_invested += entry * qty

            pct_s   = _color_pct(pct)
            gain_s  = _color_dollar(gain_d, 8)
            entry_s = _fmt_price(entry)
            last_s  = _fmt_price(last)
            stop_s  = _fmt_price(stop)
            val_s   = f"${value:>8,.0f}" if not math.isnan(value) else f"{'—':>9}"

            print(f"  {idx:>2}  {sym:<6}  {qty:>5}  "
                  f"{entry_s}  {last_s}  {pct_s}  {gain_s}  "
                  f"{stop_s}  {val_s}  {days:>3}d")

        # Section totals
        print(_COL_SEP)
        inv_s  = f"${sect_invested:>10,.0f}" if sect_invested else "—"
        pnl_c  = _GRN if sect_pnl >= 0 else _RED
        pnl_s  = f"{'+' if sect_pnl >= 0 else ''}${sect_pnl:,.0f}"
        print(f"  {'Invested':>30} : {inv_s}    P&L : {pnl_c}{pnl_s}{_RST}")

    # ── Idle strategies (no positions) — one compact line ─────────────────────
    if idle_labels:
        print(f"\n  {_DIM}Idle: {' · '.join(idle_labels)}{_RST}")

    print()
    print("  " + "─" * _W)
    print(f"  {_DIM}Ctrl+C to quit  ·  --once to print once  ·  --interval N secs{_RST}\n")


def main() -> None:
    _load_env()
    cfg = _load_config()

    parser = argparse.ArgumentParser(description="IBKR strategy-grouped dashboard")
    parser.add_argument("--once",     action="store_true", help="Print once and exit")
    parser.add_argument("--interval", type=int, default=10, help="Refresh interval in seconds")
    args = parser.parse_args()

    # Silence all ib_insync loggers before any connection so noisy paper-account
    # warnings (10089/10168 no subscription, 300 cancel race, completed orders
    # timeout) don't appear. Yahoo fallback handles missing prices.
    import logging
    for _log in ("ib_insync", "ib_insync.ib", "ib_insync.wrapper",
                 "ib_insync.client", "ib_insync.ticker"):
        logging.getLogger(_log).setLevel(logging.CRITICAL)

    from ibkr_module import ibkr_client as ic
    from ibkr_module import ibkr_state  as st

    client_id  = cfg.get("client_ids", {}).get("dashboard", 15)
    host       = cfg["host"]
    port       = cfg["port_paper"] if cfg.get("paper", True) else cfg["port_live"]
    account_id = os.environ.get("IBKR_ACCOUNT_ID", "")

    while True:
        try:
            ib = ic.connect(host, port, client_id)

            if not account_id:
                accounts   = ib.managedAccounts()
                account_id = accounts[0] if accounts else "unknown"

            summary    = ic.get_account_summary(ib, account_id)
            all_open   = st.get_open_positions()

            # Group by strategy key
            positions_by_strat: dict[str, list[dict]] = {}
            for pos in all_open:
                key = pos.get("strategy", "blend")
                positions_by_strat.setdefault(key, []).append(pos)

            # Live prices for all held symbols
            symbols      = list({p["symbol"] for p in all_open})
            live_prices  = ic.get_prices(ib, symbols) if symbols else {}

            # Full Yahoo fallback when IBKR returns nothing at all.
            yahoo_fallback = bool(symbols) and not any(
                v and v > 0 for v in live_prices.values()
            )
            # Per-symbol Yahoo fill for any individual zeros (partial IBKR failure).
            missing_price = [s for s in symbols if not live_prices.get(s)]
            if yahoo_fallback or missing_price:
                from ibkr_module.ibkr_signals import yahoo_prices
                fill_syms = symbols if yahoo_fallback else missing_price
                yp = yahoo_prices(fill_syms)
                live_prices.update({s: p for s, p in yp.items() if p and p > 0})

            ic.disconnect(ib)

            render_dashboard(cfg, summary, account_id, positions_by_strat,
                             live_prices, yahoo_fallback)

        except KeyboardInterrupt:
            print("\n  Stopped.")
            break
        except Exception as exc:
            os.system("cls" if os.name == "nt" else "clear")
            print(f"\n  [ERROR] {exc}")
            print("  Make sure IB Gateway is running on port "
                  f"{cfg.get('port_paper', 4002)} and API access is enabled.")

        if args.once:
            break

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n  Stopped.")
            break


if __name__ == "__main__":
    main()
