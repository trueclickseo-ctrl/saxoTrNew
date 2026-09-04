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
# (db_strategy_key, display_label, config_key, config_slot_field)
_STRATEGY_ROWS = [
    ("blend",            "US Blend",          "blend",     "max_positions"),
    ("reversion",        "US Reversion",      "reversion", "max_slots"),
    ("intraday",         "US Intraday Rev.",  "reversion", "max_slots"),
    ("US SMA Crossover", "US SMA Crossover",  "signals",   "max_per_strategy"),
    ("US RSI Reversal",  "US RSI Reversal",   "signals",   "max_per_strategy"),
    ("US Momentum",      "US Momentum",       "signals",   "max_per_strategy"),
    ("US Ensemble",      "US Ensemble",       "signals",   "max_per_strategy"),
]

_WIDTH = 72  # terminal width for section headers


def _max_slots(cfg: dict, cfg_key: str, slot_field: str) -> int:
    return int(cfg.get("strategies", {}).get(cfg_key, {}).get(slot_field, 5))


def _days_held(filled_at: str | None) -> int:
    if not filled_at:
        return 0
    try:
        dt = datetime.fromisoformat(filled_at.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return 0


def _gain_str(entry: float, last: float) -> str:
    if entry and entry > 0 and last and last > 0:
        pct = (last / entry - 1) * 100
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.1f}%"
    return "  n/a"


def _fmt(val: float, prefix: str = "$", decimals: int = 2) -> str:
    if not val or math.isnan(val):
        return "  n/a"
    return f"{prefix}{val:,.{decimals}f}"


def render_dashboard(cfg: dict, summary: dict, account_id: str,
                     positions_by_strat: dict[str, list[dict]],
                     live_prices: dict[str, float],
                     yahoo_fallback: bool) -> None:
    os.system("cls" if os.name == "nt" else "clear")

    mode  = "PAPER" if cfg.get("paper", True) else "LIVE"
    now   = time.strftime("%Y-%m-%d %H:%M:%S")
    total = sum(len(v) for v in positions_by_strat.values())
    price_src = "[Yahoo est. — market closed]" if yahoo_fallback else "[IBKR live prices]"

    print(f"\n  IBKR STOCKS DASHBOARD [{mode}]  {now}  {price_src}")
    print("  " + "═" * _WIDTH)

    # Account summary
    ccy = summary.get("currency", "USD")
    nl  = summary.get("net_liquidation", 0)
    cb  = summary.get("cash_balance", 0)
    u   = summary.get("unrealized_pnl", 0)
    r   = summary.get("realized_pnl", 0)
    print(f"\n  Account  : {account_id}   ({ccy})")
    print(f"  Net Liq  : {ccy} {nl:>13,.2f}    Cash    : {ccy} {cb:>13,.2f}")
    u_sign = "+" if u >= 0 else ""
    r_sign = "+" if r >= 0 else ""
    print(f"  Unreal.  : {u_sign}{ccy} {u:>12,.2f}    Realized: {r_sign}{ccy} {r:>12,.2f}")
    print(f"  Open positions: {total}")

    # Per-strategy sections
    for db_key, label, cfg_key, slot_field in _STRATEGY_ROWS:
        positions = positions_by_strat.get(db_key, [])
        max_s     = _max_slots(cfg, cfg_key, slot_field)
        used      = len(positions)
        pct_full  = used / max_s * 100 if max_s else 0
        slot_bar  = f"{'█' * used}{'░' * (max_s - used)}"

        header_left = f"  ── {label}  [{slot_bar}] {used}/{max_s}"
        pad         = max(1, _WIDTH - len(header_left) + 2)
        print(f"\n{header_left} {'─' * pad}")

        if not positions:
            print("      (no open positions)")
            continue

        # Column header
        print(f"    {'Symbol':<8}  {'Qty':>5}  {'Entry':>8}  {'Last':>8}  "
              f"{'Gain%':>7}  {'Stop':>8}  {'Days':>4}")
        print("    " + "─" * 60)

        for pos in sorted(positions, key=lambda p: p.get("filled_at") or ""):
            sym   = pos["symbol"]
            qty   = int(pos.get("qty", 0))
            entry = pos.get("fill_price") or pos.get("limit_price") or 0.0
            last  = live_prices.get(sym, 0.0)
            stop  = pos.get("stop_price") or 0.0
            days  = _days_held(pos.get("filled_at"))

            gain  = _gain_str(entry, last)
            # Colour hint via ASCII marker
            if "+" in gain:
                gain_disp = f"\033[32m{gain}\033[0m"   # green
            elif "-" in gain:
                gain_disp = f"\033[31m{gain}\033[0m"   # red
            else:
                gain_disp = gain

            entry_s = _fmt(entry)
            last_s  = _fmt(last) if last else "    n/a"
            stop_s  = _fmt(stop) if stop else "    n/a"

            print(f"    {sym:<8}  {qty:>5}  {entry_s:>8}  {last_s:>8}  "
                  f"{gain_disp:>7}  {stop_s:>8}  {days:>4}d")

    print(f"\n  {'─' * _WIDTH}")
    print(f"  Ctrl+C to quit   |   --once to print and exit   |   --interval N to change refresh\n")


def main() -> None:
    _load_env()
    cfg = _load_config()

    parser = argparse.ArgumentParser(description="IBKR strategy-grouped dashboard")
    parser.add_argument("--once",     action="store_true", help="Print once and exit")
    parser.add_argument("--interval", type=int, default=30, help="Refresh interval in seconds")
    args = parser.parse_args()

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
            yahoo_fallback = bool(symbols) and not any(
                v and v > 0 for v in live_prices.values()
            )
            if yahoo_fallback and symbols:
                from ibkr_module.ibkr_signals import yahoo_prices
                yp = yahoo_prices(symbols)
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
