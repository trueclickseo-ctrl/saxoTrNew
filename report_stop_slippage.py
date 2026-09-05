"""report_stop_slippage.py — P4 stop-slippage measurement report.

Reads data/stop_slippage.jsonl (written by forex/runner._log_stop_slippage)
and summarises the gap between intended stop_price and actual fill_price for
hard_stop exits, broken down by strategy and account.

Slippage is defined as adverse when negative (long fills below stop, short
fills above stop). Units: price, ATR multiples, R multiples.

Usage:
    python report_stop_slippage.py           # all accounts
    python report_stop_slippage.py sim       # filter to one account
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Any

BASE = os.path.dirname(__file__)
LOG_PATH = os.path.join(BASE, "data", "stop_slippage.jsonl")


def _load(account_filter: str | None = None) -> list[dict]:
    if not os.path.exists(LOG_PATH):
        print(f"[stop-slippage] {LOG_PATH} not found — no data yet (file is written on first hard_stop close)")
        return []
    rows: list[dict] = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if account_filter and row.get("account") != account_filter:
                continue
            rows.append(row)
    return rows


def _stats(vals: list[float]) -> dict[str, float]:
    if not vals:
        return {}
    vals = sorted(vals)
    n = len(vals)
    mean = sum(vals) / n
    median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    adverse = [v for v in vals if v < 0]
    return {
        "n": n,
        "mean": round(mean, 4),
        "median": round(median, 4),
        "adverse_pct": round(len(adverse) / n * 100, 1),
        "min": round(vals[0], 4),
        "max": round(vals[-1], 4),
    }


def _fmt_row(label: str, s: dict[str, Any], unit: str) -> str:
    if not s:
        return f"  {label:<22} n=0"
    return (
        f"  {label:<22} n={s['n']}  mean={s['mean']:+.4f}{unit}  "
        f"median={s['median']:+.4f}{unit}  "
        f"adverse={s['adverse_pct']:.0f}%  "
        f"[{s['min']:+.4f} .. {s['max']:+.4f}]{unit}"
    )


def report(account_filter: str | None = None) -> None:
    rows = _load(account_filter)
    if not rows:
        return

    label = f"account={account_filter}" if account_filter else "all accounts"
    print(f"\n\033[1mStop-slippage report\033[0m  \033[2m({len(rows)} hard_stop exits, {label})\033[0m")
    print("  Negative = adverse (long filled below stop, short above stop)\n")

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_strategy[r.get("strategy", "?")].append(r)

    for strat, strat_rows in sorted(by_strategy.items()):
        slip_price = [r["slippage_price"] for r in strat_rows if r.get("slippage_price") is not None]
        slip_atr   = [r["slippage_atr"]   for r in strat_rows if r.get("slippage_atr")   is not None]
        slip_r     = [r["slippage_r"]     for r in strat_rows if r.get("slippage_r")     is not None]

        print(f"\033[96m{strat}\033[0m  ({len(strat_rows)} exits)")
        print(_fmt_row("price", _stats(slip_price), ""))
        if slip_atr:
            print(_fmt_row("ATR multiples", _stats(slip_atr), "×"))
        if slip_r:
            print(_fmt_row("R multiples", _stats(slip_r), "R"))
        print()

    # Overall
    all_price = [r["slippage_price"] for r in rows if r.get("slippage_price") is not None]
    all_atr   = [r["slippage_atr"]   for r in rows if r.get("slippage_atr")   is not None]
    all_r     = [r["slippage_r"]     for r in rows if r.get("slippage_r")     is not None]
    print("\033[1mOVERALL\033[0m")
    print(_fmt_row("price", _stats(all_price), ""))
    if all_atr:
        print(_fmt_row("ATR multiples", _stats(all_atr), "×"))
    if all_r:
        print(_fmt_row("R multiples", _stats(all_r), "R"))
    print()

    print("  \033[2mInterpretation: mean slippage_r < -0.05 R → slippage is eating a measurable\033[0m")
    print("  \033[2mfraction of risk budget; investigate spread/latency at stop execution time.\033[0m")


if __name__ == "__main__":
    report(sys.argv[1] if len(sys.argv) > 1 else None)
