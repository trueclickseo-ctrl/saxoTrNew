"""
report_gap_weekend_by_type.py
-------------------------------
Per-gap-type performance breakdown for the "gap_weekend" strategy
(forex/strategy_gap_weekend.py) -- explicit user requirement: never judge
GAPFILL Weekend by one combined win rate, always separate by gap_type
(weekly/london/newyork/tokyo).

Reads data/pnl_ledger.db directly (via pnl_tracker.get_closed_trades()),
filters to strategy == "gap_weekend", and groups by the gap_type column
added to that table 2026-08-29 specifically for this. Weekly is the only
type with any real trades until session variants are re-enabled (Phase 3)
-- see strategy_gap_weekend.py's ENABLED_SESSIONS.

Usage:
    python report_gap_weekend_by_type.py            # all-time
    python report_gap_weekend_by_type.py --since 2026-08-29
"""

import argparse
from datetime import datetime

import pnl_tracker

GREEN, RED, YELLOW, CYAN, RESET, BOLD, DIM = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m", "\033[2m"
)


def _stats_for(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wr": None, "pf": None, "expectancy": None,
                "gross_profit": 0.0, "gross_loss": 0.0, "total_pnl": 0.0}
    pnls = [float(t["realized_pnl"]) for t in trades if t.get("realized_pnl") is not None]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))
    wr = (len(wins) / len(pnls) * 100.0) if pnls else None
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else None)
    expectancy = (sum(pnls) / len(pnls)) if pnls else None
    return {
        "trades": n, "wr": wr, "pf": pf, "expectancy": expectancy,
        "gross_profit": gross_profit, "gross_loss": gross_loss,
        "total_pnl": sum(pnls),
    }


def main():
    ap = argparse.ArgumentParser(description="Per-gap-type breakdown for the gap_weekend strategy")
    ap.add_argument("--since", help="ISO date/timestamp -- only trades closed on/after this")
    ap.add_argument("--strategy", default="gap_weekend",
                     help="strategy name to filter to (default: gap_weekend; "
                          "pass 'gap' to run the same breakdown on the original)")
    args = ap.parse_args()

    closed = pnl_tracker.get_closed_trades(module="forex", limit=100000, since=args.since)
    closed = [t for t in closed if t.get("strategy") == args.strategy]

    by_type: dict[str, list] = {}
    for t in closed:
        gt = t.get("gap_type") or "(none recorded)"
        by_type.setdefault(gt, []).append(t)

    print(f"\n{BOLD}{CYAN}{'='*78}{RESET}")
    print(f"{BOLD}{CYAN}  {args.strategy.upper()} — PER-GAP-TYPE BREAKDOWN"
          f"{'  (since ' + args.since + ')' if args.since else ''}{RESET}")
    print(f"{BOLD}{CYAN}{'='*78}{RESET}")

    if not closed:
        print(f"{YELLOW}  No closed '{args.strategy}' trades yet.{RESET}\n")
        return

    header = f"  {'Gap Type':<12} {'Trades':>7} {'WR':>8} {'Profit Factor':>14} {'Expectancy':>12} {'Total P&L':>12}"
    print(f"{DIM}{header}{RESET}")
    print(f"  {'-'*74}")

    ordered_types = ["weekly", "london", "newyork", "tokyo"] + \
                    [k for k in by_type if k not in ("weekly", "london", "newyork", "tokyo")]

    for gt in ordered_types:
        trades = by_type.get(gt)
        if not trades:
            continue
        s = _stats_for(trades)
        wr_s = f"{s['wr']:.1f}%" if s['wr'] is not None else "—"
        pf_s = (f"{s['pf']:.2f}" if isinstance(s['pf'], float) and s['pf'] != float("inf")
                else "inf" if s['pf'] == float("inf") else "—")
        exp_s = f"{s['expectancy']:+.2f}" if s['expectancy'] is not None else "—"
        pnl_col = GREEN if s['total_pnl'] >= 0 else RED
        print(f"  {gt:<12} {s['trades']:>7} {wr_s:>8} {pf_s:>14} {exp_s:>12} "
              f"{pnl_col}{s['total_pnl']:>+12.2f}{RESET}")

    print(f"  {'-'*74}")
    overall = _stats_for(closed)
    overall_wr_s = f"{overall['wr']:.1f}%" if overall['wr'] is not None else "—"
    print(f"{DIM}  {'(combined)':<12} {overall['trades']:>7} {overall_wr_s:>8} "
          f"{'':>14} {'':>12} {overall['total_pnl']:>+12.2f}{RESET}")
    print(f"{DIM}  (combined row shown for reference only -- do not use it to judge the "
          f"strategy; the whole point of this report is per-type separation){RESET}\n")


if __name__ == "__main__":
    main()
