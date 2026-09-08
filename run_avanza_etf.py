"""
run_avanza_etf.py
-----------------
Avanza broad-market ETF sleeve — momentum strategy, monthly rebalance.

Ranks 10 UCITS ETFs by 3m+6m momentum score, buys top 3, exits
any position that falls below rank 5. Budget set manually in
avanza_module/config/avanza_etf_config.json (B3 design: you decide
how much to allocate per deposit).

Usage:
    python run_avanza_etf.py                      # dry-run: show plan
    python run_avanza_etf.py --execute            # place orders interactively
    python run_avanza_etf.py --scores             # show momentum scores only
    python run_avanza_etf.py --resolve            # verify universe on Avanza
    python run_avanza_etf.py --only IWDA --execute  # single ETF test

Before first run:
    1. Set budget_sek in avanza_module/config/avanza_etf_config.json
    2. Run --resolve to confirm all ETFs found on Avanza
    3. Run dry-run to see the plan
    4. Run --execute to trade
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)


def _load_env() -> None:
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
    cfg = os.path.join(_ROOT, "avanza_module", "config", "avanza_etf_config.json")
    with open(cfg, encoding="utf-8") as f:
        raw = json.load(f)
    return {
        "budget_sek":            float(raw["capital"]["budget_sek"]),
        "max_positions":         int(raw["capital"]["max_positions"]),
        "min_trade_sek":         float(raw["capital"]["min_trade_sek"]),
        "stop_pct":              float(raw["risk"]["stop_pct"]),
        "stop_sell_slippage_pct":float(raw["risk"]["stop_sell_slippage_pct"]),
        "momentum_weights":      raw["strategy"]["momentum_weights"],
    }


def main() -> None:
    _load_env()

    p = argparse.ArgumentParser(
        description="Avanza ETF sleeve — broad-market momentum, monthly rebalance"
    )
    p.add_argument("--execute",  action="store_true", help="Place orders (default: dry-run)")
    p.add_argument("--scores",   action="store_true", help="Show momentum scores and exit")
    p.add_argument("--resolve",  action="store_true", help="Verify universe on Avanza and exit")
    p.add_argument("--only",     metavar="TICKER",    help="Process only this ETF ticker")
    args = p.parse_args()

    from avanza_module import avanza_client as ac
    from avanza_module import avanza_etf_universe as universe
    from avanza_module import avanza_etf_signal   as signal
    from avanza_module import avanza_etf_executor as ex

    print("  Connecting to Avanza...", end=" ", flush=True)
    try:
        client = ac.get_client()
        print("OK")
    except Exception as exc:
        print(f"\n  ERROR: {exc}")
        sys.exit(1)

    account_id = os.environ.get("AVANZA_ACCOUNT_ID") or ac.get_isk_account_id(client)
    if not account_id:
        print("  ERROR: could not determine ISK account ID.")
        sys.exit(1)
    print(f"  Account: {account_id}")

    # ── --resolve: verify ETF universe on Avanza ──────────────────────────────
    if args.resolve:
        universe.resolve_universe(client, force_refresh=True)
        return

    # ── --scores: momentum scores only, no trading ────────────────────────────
    if args.scores:
        uni = universe.UNIVERSE
        if args.only:
            uni = [e for e in uni if e["ticker"].upper() == args.only.upper()]
        w   = {"3m": 0.5, "6m": 0.5}
        ranked = signal.compute_scores(uni, w_3m=w["3m"], w_6m=w["6m"])
        signal.print_scores(ranked, top_n=3, exit_rank=5)
        return

    # ── Rebalance (dry-run or execute) ────────────────────────────────────────
    dry_run = not args.execute
    if dry_run:
        print("  [DRY RUN] Showing plan only — pass --execute to place orders.\n")

    config = _load_config()
    ex.run_rebalance(client, account_id, config,
                     dry_run=dry_run, only_ticker=args.only)


if __name__ == "__main__":
    main()
