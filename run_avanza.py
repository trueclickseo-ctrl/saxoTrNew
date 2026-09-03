"""
run_avanza.py
-------------
Avanza Sweden ISK sleeve — semi-automatic US Blend mirror.

Reads the ATOS US Blend signal (Saxo-side scan), shows what to BUY/SELL
on Avanza, prompts for confirmation, and places limit orders.

NO automatic / unattended execution. Every trade requires an interactive 'y'.

Credentials from .env.avanza (see .env.avanza.example):
    AVANZA_USERNAME, AVANZA_PASSWORD, AVANZA_TOTP_SECRET, AVANZA_ACCOUNT_ID

Usage:
    python run_avanza.py                  # dry-run: show plan, place nothing
    python run_avanza.py --execute        # semi-auto: show plan, confirm each trade
    python run_avanza.py --positions      # show current Avanza positions
    python run_avanza.py --dashboard      # live dashboard (refreshes every 30s)
    python run_avanza.py --resolve-tickers AAPL MSFT NVDA  # test ticker lookup
    python run_avanza.py --info           # account summary
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
    """Load .env.avanza into os.environ (dotenv-style, no overwrite)."""
    env_file = os.path.join(_ROOT, ".env.avanza")
    if not os.path.exists(env_file):
        return
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _load_config() -> dict:
    cfg_path = os.path.join(_ROOT, "avanza_module", "config", "avanza_config.json")
    with open(cfg_path, encoding="utf-8") as f:
        raw = json.load(f)
    return {
        "budget_sek":      float(raw["capital"]["budget_sek"]),
        "max_positions":   int(raw["capital"]["max_positions"]),
        "cash_buffer_pct": float(raw["capital"]["cash_buffer_pct"]),
        "min_trade_sek":   float(raw["capital"]["min_trade_sek"]),
    }


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_positions(client, account_id: str) -> None:
    from avanza_module import avanza_client as ac
    positions = ac.get_positions(client, account_id)
    if not positions:
        print("  No open positions.")
        return
    print(f"\n  Open positions in Avanza ISK account {account_id}:")
    print(f"  {'Ticker':<10} {'Name':<25} {'Qty':>5} {'AvgCost':>8} {'Last':>8} {'Value SEK':>10} {'Gain%':>7}")
    print("  " + "-" * 80)
    for p in positions:
        g = p.get("gain_pct", 0)
        sign = "+" if g >= 0 else ""
        print(f"  {p['ticker']:<10} {p['name'][:24]:<25} {p['qty']:>5} "
              f"{p['avg_price']:>8.2f} {p['current_price']:>8.2f} "
              f"{p['value_sek']:>10,.0f} {sign}{g:>6.1f}%")


def cmd_info(client, account_id: str) -> None:
    from avanza_module import avanza_client as ac
    summary = ac.get_account_summary(client, account_id)
    print(f"\n  Avanza Account: {summary.get('account_name','')} "
          f"({summary.get('account_type','')} | ID={account_id})")
    print(f"  Value         : {summary.get('value_sek',0):>12,.0f} SEK")
    print(f"  Buying power  : {summary.get('buying_power_sek',0):>12,.0f} SEK")
    g = summary.get("total_profit_pct", 0)
    sign = "+" if g >= 0 else ""
    print(f"  Total profit  : {sign}{g:.2f}%")


def cmd_resolve_tickers(client, tickers: list[str]) -> None:
    from avanza_module import avanza_instrument_cache as ic
    print(f"\n  Resolving {len(tickers)} ticker(s) against Avanza...")
    cache = ic.load_cache()
    for ticker in tickers:
        ob_id = ic.lookup(client, ticker, cache, force_refresh=True)
        if ob_id:
            entry = cache.get(ticker, {})
            print(f"  {ticker:<8} → order_book_id={ob_id}  "
                  f"name={entry.get('name','')}  "
                  f"currency={entry.get('currency','')}  "
                  f"country={entry.get('country','')}")
        else:
            print(f"  {ticker:<8} → NOT FOUND on Avanza")
    ic.save_cache(cache)
    print("  Cache updated.")


def cmd_dashboard(client, account_id: str, interval: int = 30) -> None:
    from avanza_module import avanza_client as ac
    from avanza_module import avanza_state as state

    while True:
        os.system("cls" if os.name == "nt" else "clear")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"  AVANZA LIVE DASHBOARD  {now}  (refresh every {interval}s | Ctrl+C to quit)")
        print("  " + "=" * 66)

        try:
            summary   = ac.get_account_summary(client, account_id)
            positions = ac.get_positions(client, account_id)
            orders    = ac.get_open_orders(client)

            g = summary.get("total_profit_pct", 0)
            sign = "+" if g >= 0 else ""
            print(f"\n  Account  : {summary.get('account_name','')} "
                  f"({summary.get('account_type','')})")
            print(f"  Value    : {summary.get('value_sek',0):>12,.0f} SEK")
            print(f"  Cash     : {summary.get('buying_power_sek',0):>12,.0f} SEK")
            print(f"  P&L      : {sign}{g:.2f}%")

            if positions:
                print(f"\n  Positions ({len(positions)}):")
                print(f"  {'Ticker':<8} {'Qty':>5} {'Avg':>8} {'Last':>8} {'SEK':>10} {'%':>7}")
                print("  " + "-" * 52)
                for p in positions:
                    gp = p.get("gain_pct", 0)
                    s2 = "+" if gp >= 0 else ""
                    print(f"  {p['ticker']:<8} {p['qty']:>5} {p['avg_price']:>8.2f} "
                          f"{p['current_price']:>8.2f} {p['value_sek']:>10,.0f} {s2}{gp:>6.1f}%")
            else:
                print("\n  No open positions.")

            if orders:
                print(f"\n  Open Orders ({len(orders)}):")
                for o in orders:
                    print(f"  {o['side']:4s} {o['ticker']:<8} qty={o['qty']:>4}  @ {o['price']:.2f}")

            st = state.read_status()
            if st.get("timestamp"):
                print(f"\n  Last signal: {st.get('timestamp','')}  "
                      f"source={st.get('signal_source','')}")
                print(f"  Target basket: {', '.join(st.get('signal_tickers',[])[:8])}")

        except Exception as exc:
            print(f"\n  [ERROR] {exc}")

        print(f"\n  Today P&L: {state.get_today_pnl_sek():+,.0f} SEK (from ledger)")
        time.sleep(interval)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _load_env()

    parser = argparse.ArgumentParser(
        description="Avanza ISK sleeve — semi-auto US Blend mirror"
    )
    parser.add_argument("--execute",  action="store_true",
                        help="Place orders interactively (default: dry-run only)")
    parser.add_argument("--positions", action="store_true",
                        help="Show current Avanza positions and exit")
    parser.add_argument("--info",      action="store_true",
                        help="Show account summary and exit")
    parser.add_argument("--dashboard", action="store_true",
                        help="Live refreshing dashboard")
    parser.add_argument("--resolve-tickers", nargs="+", metavar="TICKER",
                        help="Test ticker → order_book_id lookup and exit")
    parser.add_argument("--interval",  type=int, default=30,
                        help="Dashboard refresh interval in seconds (default 30)")
    args = parser.parse_args()

    from avanza_module import avanza_client as ac
    from avanza_module import avanza_executor as ex

    print("  Connecting to Avanza...", end=" ", flush=True)
    try:
        client = ac.get_client()
        print("OK")
    except EnvironmentError as exc:
        print(f"\n  ERROR: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  Login failed: {exc}")
        sys.exit(1)

    account_id = os.environ.get("AVANZA_ACCOUNT_ID") or ac.get_isk_account_id(client)
    if not account_id:
        print("  ERROR: Could not determine ISK account ID. Set AVANZA_ACCOUNT_ID in .env.avanza.")
        sys.exit(1)
    print(f"  Account: {account_id}")

    # ── Dispatch ──────────────────────────────────────────────────────────────
    if args.positions:
        cmd_positions(client, account_id)
        return

    if args.info:
        cmd_info(client, account_id)
        return

    if args.resolve_tickers:
        cmd_resolve_tickers(client, args.resolve_tickers)
        return

    if args.dashboard:
        cmd_dashboard(client, account_id, interval=args.interval)
        return

    # Default: rebalance (dry-run unless --execute)
    dry_run = not args.execute
    if dry_run:
        print("  [DRY RUN] Showing plan only — pass --execute to place orders.\n")

    config = _load_config()
    ex.run_rebalance(client, account_id, config, dry_run=dry_run)


if __name__ == "__main__":
    main()
