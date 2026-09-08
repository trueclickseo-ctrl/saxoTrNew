"""
reports/execution_quality_report.py
------------------------------------
Empirical analysis of execution quality from data/execution_quality.db.

Compares fill quality across window lengths, modules, and strategies:
  - Avg/median slippage vs signal price and vs order price
  - % of entries that timed out (no condition satisfied in window)
  - Avg spread at entry
  - Avg time to fill
  - Per-symbol and per-strategy breakdowns

Usage
-----
    python reports/execution_quality_report.py
    python reports/execution_quality_report.py --module forex
    python reports/execution_quality_report.py --symbol EURUSD
    python reports/execution_quality_report.py --window 30
    python reports/execution_quality_report.py --raw         # dump all rows
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

_ROOT    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_PATH = os.path.join(_ROOT, "data", "execution_quality.db")


def _conn() -> sqlite3.Connection:
    if not os.path.exists(_DB_PATH):
        sys.exit(f"[execution_quality_report] DB not found: {_DB_PATH}\n"
                 f"  No executions recorded yet — run ATOS with --live to generate data.")
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _fmt(v, fmt=".4f", na="—"):
    return format(v, fmt) if v is not None else na


def _pct(v, na="—"):
    return f"{v*100:.2f}%" if v is not None else na


def _section(title: str) -> None:
    print(f"\n{'═'*70}")
    print(f"  {title}")
    print('═'*70)


def _summary_block(rows: list, label: str) -> None:
    if not rows:
        print(f"  (no data for {label})")
        return
    n           = len(rows)
    n_filled    = sum(1 for r in rows if r["fill_price"] is not None)
    n_timed_out = sum(1 for r in rows if r["timed_out"])
    slippages_s = [r["slippage_vs_signal"] for r in rows if r["slippage_vs_signal"] is not None]
    slippages_o = [r["slippage_vs_order"]  for r in rows if r["slippage_vs_order"]  is not None]
    spreads     = [r["spread_pct_at_entry"] for r in rows if r["spread_pct_at_entry"] is not None]
    ttf         = [r["time_to_fill_s"]      for r in rows if r["time_to_fill_s"]      is not None]
    elapsed     = [r["elapsed_seconds"]     for r in rows if r["elapsed_seconds"]      is not None]
    n_quotes    = [r["n_quotes"]            for r in rows if r["n_quotes"]             is not None]

    def avg(lst):  return sum(lst)/len(lst) if lst else None
    def med(lst):
        if not lst: return None
        s = sorted(lst); m = len(s)//2
        return (s[m-1]+s[m])/2 if len(s)%2==0 else s[m]

    print(f"\n  {label}  (n={n}, fills confirmed={n_filled})")
    print(f"  {'Timed-out entries':<30} {n_timed_out}/{n} ({n_timed_out/n*100:.1f}%)")
    print(f"  {'Avg window elapsed (s)':<30} {_fmt(avg(elapsed), '.1f')}")
    print(f"  {'Avg quotes observed':<30} {_fmt(avg(n_quotes), '.1f')}")
    print(f"  {'Avg spread at entry (%)':<30} {_fmt(avg(spreads), '.4f')}")
    print(f"  {'Avg slippage vs signal':<30} {_fmt(avg(slippages_s), '.5f')}")
    print(f"  {'Med slippage vs signal':<30} {_fmt(med(slippages_s), '.5f')}")
    print(f"  {'Avg slippage vs order':<30} {_fmt(avg(slippages_o), '.5f')}")
    print(f"  {'Med slippage vs order':<30} {_fmt(med(slippages_o), '.5f')}")
    print(f"  {'Avg time-to-fill (s)':<30} {_fmt(avg(ttf), '.2f')}")


def run(args: argparse.Namespace) -> None:
    con  = _conn()
    base = "SELECT * FROM executions WHERE 1=1"
    params: list = []
    if args.module:
        base += " AND module=?";   params.append(args.module)
    if args.symbol:
        base += " AND symbol=?";   params.append(args.symbol.upper())
    if args.window:
        base += " AND window_seconds=?"; params.append(float(args.window))

    rows = [dict(r) for r in con.execute(base, params).fetchall()]
    con.close()

    if not rows:
        print(f"[execution_quality_report] No rows match the filters.")
        return

    if args.raw:
        cols = list(rows[0].keys())
        skip = {"quotes_json"}
        hdr  = [c for c in cols if c not in skip]
        print("\t".join(hdr))
        for r in rows:
            print("\t".join(str(r[c]) if r[c] is not None else "" for c in hdr))
        return

    print(f"\nExecution Quality Report — {len(rows)} executions")

    # ── Overall ───────────────────────────────────────────────────────────────
    _section("Overall")
    _summary_block(rows, "All executions")

    # ── By window length ──────────────────────────────────────────────────────
    _section("By window_seconds (A/B comparison)")
    windows = sorted({r["window_seconds"] for r in rows})
    for w in windows:
        sub = [r for r in rows if r["window_seconds"] == w]
        _summary_block(sub, f"window={w:.0f}s")

    # ── By module ─────────────────────────────────────────────────────────────
    _section("By module")
    modules = sorted({r["module"] for r in rows})
    for m in modules:
        sub = [r for r in rows if r["module"] == m]
        _summary_block(sub, f"module={m}")

    # ── By strategy ───────────────────────────────────────────────────────────
    _section("By strategy")
    strategies = sorted({r["strategy"] for r in rows})
    for s in strategies:
        sub = [r for r in rows if r["strategy"] == s]
        _summary_block(sub, f"strategy={s}")

    # ── Top symbols by slippage ───────────────────────────────────────────────
    _section("Worst slippage by symbol (signal)")
    sym_slip: dict[str, list] = {}
    for r in rows:
        if r["slippage_vs_signal"] is not None:
            sym_slip.setdefault(r["symbol"], []).append(r["slippage_vs_signal"])
    ranked = sorted(sym_slip.items(), key=lambda kv: sum(kv[1])/len(kv[1]), reverse=True)
    print(f"\n  {'Symbol':<12} {'n':>5} {'avg_slip':>12}")
    for sym, slips in ranked[:20]:
        avg_s = sum(slips)/len(slips)
        print(f"  {sym:<12} {len(slips):>5} {avg_s:>12.5f}")

    # ── Timed-out vs condition-met comparison ─────────────────────────────────
    _section("Timed-out vs condition-met fill quality")
    timed  = [r for r in rows if r["timed_out"] and r["slippage_vs_signal"] is not None]
    cond   = [r for r in rows if not r["timed_out"] and r["slippage_vs_signal"] is not None]
    def avg_slip(lst): return sum(r["slippage_vs_signal"] for r in lst)/len(lst) if lst else None
    print(f"\n  Condition-met entries: n={len(cond)}, avg slippage={_fmt(avg_slip(cond), '.5f')}")
    print(f"  Timed-out entries:     n={len(timed)}, avg slippage={_fmt(avg_slip(timed), '.5f')}")

    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Execution quality analysis")
    ap.add_argument("--module",  help="Filter: forex | stocks")
    ap.add_argument("--symbol",  help="Filter: e.g. EURUSD, AAPL")
    ap.add_argument("--window",  type=float, help="Filter: window_seconds (15 / 30 / 60)")
    ap.add_argument("--raw",     action="store_true", help="Dump raw rows as TSV")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
