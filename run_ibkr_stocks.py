"""
run_ibkr_stocks.py
------------------
IBKR stocks sleeve — semi-automatic US Blend mirror via IB Gateway / TWS.

Reads the US Blend signal from data/stocks_live_status.json,
connects to IB Gateway on localhost, and manages positions.

NO automatic / unattended execution. Every trade requires an interactive 'y'.
Claude never runs --execute or places IBKR trades.

Prerequisites:
    pip install ib_insync
    IB Gateway or TWS running on this machine
    API enabled: TWS → Edit → Global Configuration → API → Settings
      ✓ Enable ActiveX and Socket Clients  |  Socket port 7497 (paper) / 7496 (live)

Credentials: IBKR_ACCOUNT_ID in .env.ibkr  (no username/password needed here —
    TWS/IB Gateway handles auth when you log in to the desktop app)

Usage:
    python run_ibkr_stocks.py                    # dry-run: show plan, place nothing
    python run_ibkr_stocks.py --execute          # semi-auto: confirm each trade
    python run_ibkr_stocks.py --positions        # show current IBKR positions
    python run_ibkr_stocks.py --info             # account summary
    python run_ibkr_stocks.py --trail-stops      # dry-run trail-stop check
    python run_ibkr_stocks.py --trail-stops --execute  # ratchet stops
    python run_ibkr_stocks.py --dashboard        # live refreshing dashboard
    python run_ibkr_stocks.py --paper            # force paper port 7497 (default from config)
    python run_ibkr_stocks.py --live             # force live port 7496
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

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


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_positions(ib, account_id: str) -> None:
    from ibkr_module import ibkr_client as ic
    positions = ic.get_positions(ib, account_id)
    if not positions:
        print("  No open IBKR positions.")
        return
    prices = ic.get_prices(ib, [p["symbol"] for p in positions])
    print(f"\n  IBKR positions (account {account_id}):")
    print(f"  {'Symbol':<8} {'Qty':>5} {'AvgCost':>8} {'Last':>8} {'Value':>10} {'Gain%':>7}")
    print("  " + "-" * 55)
    for p in positions:
        last  = prices.get(p["symbol"], 0)
        value = last * p["qty"]
        gain  = (last / p["avg_cost"] - 1) * 100 if p["avg_cost"] > 0 else 0
        sign  = "+" if gain >= 0 else ""
        print(f"  {p['symbol']:<8} {p['qty']:>5} {p['avg_cost']:>8.2f} "
              f"{last:>8.2f} {value:>10,.0f} {sign}{gain:>6.1f}%")


def cmd_info(ib, account_id: str) -> None:
    from ibkr_module import ibkr_client as ic
    s = ic.get_account_summary(ib, account_id)
    print(f"\n  IBKR Account: {account_id}")
    print(f"  Net Liquidation : ${s['net_liquidation']:>12,.2f}")
    print(f"  Cash Balance    : ${s['cash_balance']:>12,.2f}")
    print(f"  Buying Power    : ${s['buying_power']:>12,.2f}")
    u = s['unrealized_pnl']
    r = s['realized_pnl']
    print(f"  Unrealized P&L  : {'+'if u>=0 else''}${u:>11,.2f}")
    print(f"  Realized P&L    : {'+'if r>=0 else''}${r:>11,.2f}")


def cmd_dashboard(ib, account_id: str, interval: int = 30) -> None:
    from ibkr_module import ibkr_client as ic
    from ibkr_module import ibkr_state as st
    while True:
        os.system("cls" if os.name == "nt" else "clear")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"  IBKR STOCKS DASHBOARD  {now}  (refresh {interval}s | Ctrl+C to quit)")
        print("  " + "=" * 60)
        try:
            summary   = ic.get_account_summary(ib, account_id)
            positions = ic.get_positions(ib, account_id)
            print(f"\n  Net Liq  : ${summary['net_liquidation']:>12,.2f}")
            print(f"  Cash     : ${summary['cash_balance']:>12,.2f}")
            u = summary['unrealized_pnl']
            print(f"  Unreal.  : {'+'if u>=0 else''}${u:>11,.2f}")

            if positions:
                prices = ic.get_prices(ib, [p["symbol"] for p in positions])
                print(f"\n  Positions ({len(positions)}):")
                print(f"  {'Sym':<8} {'Qty':>5} {'Avg':>8} {'Last':>8} {'Gain%':>7}")
                print("  " + "-" * 40)
                for p in positions:
                    last = prices.get(p["symbol"], 0)
                    g    = (last / p["avg_cost"] - 1) * 100 if p["avg_cost"] > 0 else 0
                    print(f"  {p['symbol']:<8} {p['qty']:>5} {p['avg_cost']:>8.2f} "
                          f"{last:>8.2f} {'+'if g>=0 else''}{g:>6.1f}%")
            else:
                print("\n  No open positions.")

            today_pnl = st.get_today_pnl_usd()
            print(f"\n  Today P&L: {'+'if today_pnl>=0 else''}${today_pnl:,.2f} (from ledger)")

        except Exception as exc:
            print(f"\n  [ERROR] {exc}")
        ib.sleep(interval)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_env()
    cfg = _load_config()

    parser = argparse.ArgumentParser(description="IBKR stocks sleeve — US Blend mirror")
    parser.add_argument("--execute",     action="store_true",
                        help="Place orders interactively (default: dry-run)")
    parser.add_argument("--paper",       action="store_true",
                        help="Force paper port 7497 (default if config paper=true)")
    parser.add_argument("--live",        action="store_true",
                        help="Force live port 7496")
    parser.add_argument("--positions",   action="store_true")
    parser.add_argument("--info",        action="store_true")
    parser.add_argument("--trail-stops", action="store_true")
    parser.add_argument("--dashboard",   action="store_true")
    parser.add_argument("--interval",    type=int, default=30)
    args = parser.parse_args()

    if args.live and args.paper:
        print("ERROR: cannot pass both --paper and --live")
        sys.exit(1)

    # Resolve port
    if args.live:
        port      = cfg["port_live"]
        is_paper  = False
    else:
        port      = cfg["port_paper"]
        is_paper  = True

    host      = cfg["host"]
    client_id = cfg["client_id"]
    account_id = os.environ.get("IBKR_ACCOUNT_ID", "")

    mode_label = "PAPER" if is_paper else "LIVE"
    print(f"  Connecting to IB Gateway [{mode_label}] {host}:{port} clientId={client_id}...")

    from ibkr_module import ibkr_client as ic
    from ibkr_module import ibkr_executor as ex

    try:
        ib = ic.connect(host, port, client_id)
    except Exception as exc:
        print(f"\n  ERROR: Could not connect to IB Gateway: {exc}")
        print("  Make sure IB Gateway or TWS is running and API access is enabled.")
        sys.exit(1)

    print("  Connected.")

    # Auto-detect account if not set
    if not account_id:
        accounts = ib.managedAccounts()
        account_id = accounts[0] if accounts else ""
        if account_id:
            print(f"  Account: {account_id} (auto-detected)")
        else:
            print("  ERROR: Could not determine account ID. Set IBKR_ACCOUNT_ID in .env.ibkr")
            ic.disconnect(ib)
            sys.exit(1)
    else:
        print(f"  Account: {account_id}")

    try:
        if args.positions:
            cmd_positions(ib, account_id)

        elif args.info:
            cmd_info(ib, account_id)

        elif args.dashboard:
            cmd_dashboard(ib, account_id, args.interval)

        elif args.trail_stops:
            ex.trail_stops(ib, account_id, cfg, dry_run=not args.execute)

        else:
            dry_run = not args.execute
            if dry_run:
                print("  [DRY RUN] Showing plan only — pass --execute to place orders.\n")
            ex.run_rebalance(ib, account_id, cfg, dry_run=dry_run)

    finally:
        ic.disconnect(ib)


if __name__ == "__main__":
    main()
