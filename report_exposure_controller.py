"""report_exposure_controller.py — Phase A exposure controller audit report.

Reads data/exposure_controller.jsonl and shows:
  - Total blocks by reason type (pair_cap / cluster_cap / risk_cap)
  - Which currency clusters are being hit most
  - Which symbols are being blocked most
  - Book risk % trend over time (how loaded the book is each cycle)
  - False-positive candidates: signals blocked that later closed as winners

Usage:
    python report_exposure_controller.py              # all accounts
    python report_exposure_controller.py sim          # filter by account
    python report_exposure_controller.py --tighten    # suggest tighter limits
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
LOG  = os.path.join(BASE, "data", "exposure_controller.jsonl")
LEDGER = os.path.join(BASE, "data", "pnl_ledger.db")

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)


def _load(account_filter: str | None) -> list[dict]:
    if not os.path.exists(LOG):
        print(f"{Y}No data yet — data/exposure_controller.jsonl not found."
              f"\nController writes on the first blocked signal (next SIM scan).{X}")
        return []
    rows = []
    with open(LOG, encoding="utf-8") as f:
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


def _closed_pnl_by_sym() -> dict[str, float]:
    """Return {symbol: total_realized_pnl} from the ledger (forex SIM only)."""
    if not os.path.exists(LEDGER):
        return {}
    import sqlite3
    con = sqlite3.connect(LEDGER)
    rows = con.execute(
        "SELECT symbol, SUM(realized_pnl) FROM trades "
        "WHERE module='forex' AND exit_price IS NOT NULL "
        "GROUP BY symbol"
    ).fetchall()
    con.close()
    return {sym: pnl or 0.0 for sym, pnl in rows}


def _reason_type(reason: str) -> str:
    if reason.startswith("pair_cap"):
        return "pair_cap"
    if reason.startswith("cluster_cap"):
        return "cluster_cap"
    if reason.startswith("risk_cap"):
        return "risk_cap"
    return "other"


def _extract_cluster(reason: str) -> str:
    """e.g. 'cluster_cap: NZD_long already at 8 positions' → 'NZD_long'"""
    if "cluster_cap" not in reason:
        return ""
    parts = reason.split(":")
    if len(parts) < 2:
        return ""
    return parts[1].strip().split()[0]


def report(account_filter: str | None = None, suggest_tighten: bool = False) -> None:
    rows = _load(account_filter)
    if not rows:
        return

    label = f"account={account_filter}" if account_filter else "all accounts"
    n = len(rows)
    print(f"\n{B}Exposure Controller Report{X}  {DIM}({n} blocks logged, {label}){X}")

    # ── 1. Blocks by reason type ─────────────────────────────────────────
    type_counts = Counter(_reason_type(r["reason"]) for r in rows)
    print(f"\n{B}Blocks by type:{X}")
    for rtype, cnt in type_counts.most_common():
        bar = "█" * min(cnt, 40)
        print(f"  {rtype:<20} {cnt:>4}  {C}{bar}{X}")

    # ── 2. Top blocked currency clusters ─────────────────────────────────
    cluster_blocks = Counter(
        _extract_cluster(r["reason"]) for r in rows if "cluster_cap" in r["reason"]
    )
    cluster_blocks.pop("", None)
    if cluster_blocks:
        print(f"\n{B}Top blocked currency clusters:{X}")
        for cluster, cnt in cluster_blocks.most_common(10):
            print(f"  {cluster:<20} {cnt:>4} blocks")

    # ── 3. Top blocked symbols ────────────────────────────────────────────
    sym_blocks = Counter(r["sym"] for r in rows)
    print(f"\n{B}Top blocked symbols (top 15):{X}")
    closed_pnl = _closed_pnl_by_sym()
    for sym, cnt in sym_blocks.most_common(15):
        pnl = closed_pnl.get(sym)
        pnl_str = f"  {G if pnl and pnl > 0 else R}closed P&L {pnl:>+.1f} EUR{X}" if pnl is not None else ""
        print(f"  {sym:<12} {cnt:>4} blocks{pnl_str}")

    # ── 4. Book risk % over time ──────────────────────────────────────────
    risk_pct_rows = [
        (r["ts"][:10], 100.0 * r["book_risk_eur"] / r["equity_eur"] if r.get("equity_eur") else None)
        for r in rows if r.get("equity_eur")
    ]
    if risk_pct_rows:
        by_day: dict[str, list[float]] = defaultdict(list)
        for day, pct in risk_pct_rows:
            if pct is not None:
                by_day[day].append(pct)
        print(f"\n{B}Book open-risk % of equity (daily avg at block time):{X}")
        for day in sorted(by_day)[-7:]:
            avg = sum(by_day[day]) / len(by_day[day])
            bar = "█" * int(avg / 2)
            colour = R if avg > 25 else Y if avg > 15 else G
            print(f"  {day}  {colour}{avg:>5.1f}%  {bar}{X}")

    # ── 5. Potential false positives ──────────────────────────────────────
    # A blocked symbol that later produced a positive closed P&L in the ledger
    # is a CANDIDATE false positive (the block may have been correct, or it may
    # have missed a winner). Surface for human review.
    fp_candidates = [
        (sym, cnt, closed_pnl[sym])
        for sym, cnt in sym_blocks.most_common()
        if sym in closed_pnl and closed_pnl[sym] > 0
    ]
    if fp_candidates:
        print(f"\n{Y}False-positive candidates (blocked symbol with positive closed P&L):{X}")
        print(f"  {DIM}Review: were these blocks justified or did the controller miss winners?{X}")
        for sym, cnt, pnl in fp_candidates[:10]:
            print(f"  {sym:<12} {cnt:>3} blocks  {G}+{pnl:.1f} EUR closed{X}")

    # ── 6. Tighten suggestions ────────────────────────────────────────────
    if suggest_tighten:
        print(f"\n{B}Limit tightening suggestions:{X}")
        print(f"  {DIM}(Based on observed block patterns — review manually before applying){X}")

        # Cluster cap: if top cluster is being hit > 5x per day on average,
        # the current cap is barely restraining it → suggest tightening by 1
        days_active = len(set(r["ts"][:10] for r in rows))
        if cluster_blocks:
            top_cluster, top_cnt = cluster_blocks.most_common(1)[0]
            avg_per_day = top_cnt / max(days_active, 1)
            current_cap = 8  # default; ideally read from config
            if avg_per_day >= 3:
                print(f"  cluster_cap: {top_cluster} hit {top_cnt}x over {days_active}d "
                      f"({avg_per_day:.1f}/day) → suggest tightening from {current_cap} to {current_cap - 1}")

        # Risk cap: if risk_cap blocks are > 10% of total, book is running hot
        if type_counts.get("risk_cap", 0) > 0.10 * n:
            print(f"  risk_cap: {type_counts['risk_cap']} blocks ({100*type_counts['risk_cap']/n:.0f}% of total) "
                  f"→ consider reducing max_open_risk_pct_equity from 30.0 to 25.0")

        # Pair cap: if a symbol has > 3 blocks and closed P&L is negative,
        # the cap is protecting us → keep or tighten to 1
        pair_cap_syms = [
            (sym, cnt, closed_pnl.get(sym, 0))
            for sym, cnt in sym_blocks.most_common()
            if cnt >= 3 and _reason_type(
                next(r["reason"] for r in rows if r["sym"] == sym)
            ) == "pair_cap"
        ]
        for sym, cnt, pnl in pair_cap_syms[:3]:
            pnl_col = G if pnl > 0 else R
            print(f"  pair_cap {sym}: {cnt} blocks, closed P&L {pnl_col}{pnl:+.1f} EUR{X} "
                  f"{'→ cap protecting (keep)' if pnl < 0 else '→ may be too tight (review)'}")

    # ── Summary ───────────────────────────────────────────────────────────
    print(f"\n{B}{'─'*60}{X}")
    days = len(set(r["ts"][:10] for r in rows))
    print(f"  {days} day(s) of data  |  {n} total blocks  |  "
          f"{n / max(days, 1):.1f} blocks/day avg")
    print(f"  Run with --tighten to see limit adjustment suggestions.")
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    tighten = "--tighten" in args
    acct = next((a for a in args if not a.startswith("--")), None)
    report(account_filter=acct, suggest_tighten=tighten)
