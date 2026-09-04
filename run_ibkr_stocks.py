"""
run_ibkr_stocks.py
------------------
IBKR stocks sleeve — all ATOS strategies via IB Gateway.

Strategies (all run signal generation from Yahoo Finance — no Saxo):
  blend      US cross-sectional momentum, fortnightly rebalance ($6k budget, 8 slots)
  reversion  US mean reversion, entries + exits ($50k budget, 15 slots)
  intraday   Intraday reversion variant (US market hours only)

NO automatic / unattended execution. Every trade requires an interactive 'y'.
Claude never runs --execute or places IBKR trades.

Prerequisites:
    pip install ib_insync yfinance
    IB Gateway running on this machine (port 4002 paper / 4001 live)
    API access enabled in Gateway: uncheck Read-Only API

Usage:
    python run_ibkr_stocks.py                               # blend dry-run
    python run_ibkr_stocks.py --strategy reversion          # reversion entries dry-run
    python run_ibkr_stocks.py --strategy reversion --exits  # check exits
    python run_ibkr_stocks.py --strategy intraday           # intraday scan dry-run
    python run_ibkr_stocks.py --execute                     # place orders (confirm each)
    python run_ibkr_stocks.py --positions                   # show IBKR positions
    python run_ibkr_stocks.py --info                        # account summary
    python run_ibkr_stocks.py --trail-stops [--execute]     # ratchet stop-losses
    python run_ibkr_stocks.py --dashboard                   # live dashboard
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
    ccy = s.get("currency", "USD")
    print(f"\n  IBKR Account: {account_id}  ({ccy})")
    print(f"  Net Liquidation  : {ccy} {s['net_liquidation']:>14,.2f}")
    print(f"  Cash Balance     : {ccy} {s['cash_balance']:>14,.2f}")
    print(f"  Buying Power     : {ccy} {s['buying_power']:>14,.2f}")
    print(f"  Excess Liquidity : {ccy} {s.get('excess_liquidity', 0):>14,.2f}")
    u = s['unrealized_pnl']
    r = s['realized_pnl']
    print(f"  Unrealized P&L   : {'+'if u>=0 else''}{ccy} {u:>13,.2f}")
    print(f"  Realized P&L     : {'+'if r>=0 else''}{ccy} {r:>13,.2f}")


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

    parser = argparse.ArgumentParser(description="IBKR stocks sleeve — all ATOS strategies")
    parser.add_argument("--strategy",    choices=["blend", "reversion", "intraday"],
                        default="blend",
                        help="Which strategy to run (default: blend)")
    parser.add_argument("--exits",       action="store_true",
                        help="Check exits for the reversion strategy (ignored for blend)")
    parser.add_argument("--execute",     action="store_true",
                        help="Place orders interactively (default: dry-run)")
    parser.add_argument("--paper",       action="store_true",
                        help="Force paper port 4002 (default if config paper=true)")
    parser.add_argument("--live",        action="store_true",
                        help="Force live port 4001")
    parser.add_argument("--positions",   action="store_true")
    parser.add_argument("--info",        action="store_true")
    parser.add_argument("--trail-stops", action="store_true")
    parser.add_argument("--dashboard",   action="store_true")
    parser.add_argument("--interval",    type=int, default=30)
    parser.add_argument("--client-id",   type=int, default=None,
                        help="IB Gateway client ID override (default: per-strategy from config)")
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

    host = cfg["host"]

    # Per-strategy client IDs prevent "client id already in use" when strategies
    # run simultaneously or back-to-back (IB Gateway holds a slot briefly after disconnect).
    client_ids = cfg.get("client_ids", {})
    if args.client_id is not None:
        client_id = args.client_id
    elif args.trail_stops:
        client_id = client_ids.get("trail", cfg["client_id"])
    elif args.info or args.positions:
        client_id = client_ids.get("info", cfg["client_id"])
    elif args.dashboard:
        client_id = client_ids.get("dashboard", cfg["client_id"])
    else:
        client_id = client_ids.get(args.strategy, cfg["client_id"])

    account_id = os.environ.get("IBKR_ACCOUNT_ID", "")

    # ── Pre-generate Yahoo Finance signals BEFORE connecting to IB Gateway ────
    # IB Gateway holds the clientId slot for ~30s after disconnect. If the
    # Yahoo download (~30s for 424 tickers) happens inside the executor while
    # the connection is open, rapid back-to-back runs hit "client id already in
    # use" (Error 326). Pre-generating here means the connection is held for
    # only ~3-5s (account lookup + positions + order placement).
    from ibkr_module import ibkr_signals as sig

    pre_signal     = None   # blend
    pre_candidates = None   # reversion / intraday
    pre_indicators = None   # reversion exits

    needs_signal = (
        not args.positions and not args.info and
        not args.dashboard and not args.trail_stops
    )
    if needs_signal:
        if args.strategy == "blend":
            print("\n  Pre-generating US Blend signal (Yahoo Finance)...")
            pre_signal = sig.blend_targets()

        elif args.strategy == "reversion":
            if args.exits:
                from ibkr_module import ibkr_state as _st
                open_syms = [p["symbol"] for p in _st.get_open_positions("reversion")]
                if open_syms:
                    print(f"\n  Pre-generating exit indicators for "
                          f"{len(open_syms)} open position(s)...")
                    pre_indicators = sig.reversion_exit_indicators(open_syms)
                else:
                    print("\n  No open reversion positions — skipping signal fetch.")
            else:
                print("\n  Pre-generating US Reversion candidates (Yahoo Finance)...")
                pre_candidates = sig.reversion_candidates()

        elif args.strategy == "intraday":
            print("\n  Pre-generating intraday reversion candidates (Yahoo Finance)...")
            pre_candidates = sig.intraday_candidates()

    # ── Connect to IB Gateway (short window now) ──────────────────────────────
    mode_label = "PAPER" if is_paper else "LIVE"
    print(f"\n  Connecting to IB Gateway [{mode_label}] {host}:{port} clientId={client_id}...")

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

        elif args.strategy == "blend":
            dry_run = not args.execute
            if dry_run:
                print("  [DRY RUN] Showing blend plan — pass --execute to place orders.\n")
            ex.run_rebalance(ib, account_id, cfg, dry_run=dry_run, signal=pre_signal)

        elif args.strategy == "reversion":
            dry_run = not args.execute
            if dry_run:
                print("  [DRY RUN] pass --execute to place orders.\n")
            if args.exits:
                ex.run_reversion_exits(ib, account_id, cfg, dry_run=dry_run,
                                       indicators=pre_indicators)
            else:
                ex.run_reversion_entries(ib, account_id, cfg, dry_run=dry_run,
                                         intraday=False, candidates=pre_candidates)

        elif args.strategy == "intraday":
            dry_run = not args.execute
            if dry_run:
                print("  [DRY RUN] pass --execute to place orders.\n")
            ex.run_reversion_entries(ib, account_id, cfg, dry_run=dry_run,
                                     intraday=True, candidates=pre_candidates)

    finally:
        ic.disconnect(ib)


if __name__ == "__main__":
    main()
