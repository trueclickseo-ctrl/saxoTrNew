"""report_phase_c_counterfactual.py — Phase C gate evidence.

Quantifies the give-back saved (or gains lost) if the Exit Copilot's
EXIT_NOW decisions had been acted on instead of letting the position run
to its natural stop/RSI exit.

For each EXIT_NOW decision logged in data/ai_exit_decisions.jsonl:
  - est_pnl_eur_at_decision  = r_now × risk_eur  (what we'd have banked)
  - actual_pnl_eur           = realized P&L from pnl_ledger.db
  - delta = est − actual      (positive = give-back saved; negative = left gains)

The Phase C gate: net delta positive with ≥10 matched EXIT_NOW trades.

Usage:
    python report_phase_c_counterfactual.py [sim]
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict

BASE     = os.path.dirname(os.path.abspath(__file__))
LOG      = os.path.join(BASE, "data", "ai_exit_decisions.jsonl")
LEDGER   = os.path.join(BASE, "data", "pnl_ledger.db")

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)

_MODULE_MAP = {
    "sim":      "forex",
    "ai_sim":   "forex_ai",
    "live":     "forex_live",
    "live_eur": "forex_live_eur",
}


def _load(account: str) -> list[dict]:
    if not os.path.exists(LOG):
        print(f"{Y}No exit decisions yet — data/ai_exit_decisions.jsonl missing."
              f"\nExit Copilot writes on the first ≥1R profitable position evaluated.{X}")
        return []
    rows = []
    with open(LOG, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if row.get("account") == account:
                rows.append(row)
    return rows


def _closed_trades(account: str) -> dict:
    """{(strategy, symbol, entry_date): realized_pnl}"""
    if not os.path.exists(LEDGER):
        return {}
    module = _MODULE_MAP.get(account, "forex")
    con = sqlite3.connect(LEDGER)
    rows = con.execute(
        "SELECT strategy, symbol, realized_pnl, timestamp_open "
        "FROM trades WHERE module=? AND exit_price IS NOT NULL",
        (module,),
    ).fetchall()
    con.close()
    by_key: dict = defaultdict(list)
    for strat, sym, pnl, t_open in rows:
        d = (t_open or "")[:10]
        by_key[(strat, sym, d)].append(float(pnl or 0))
    return by_key


def _match(row: dict, closed: dict) -> float | None:
    key = (row.get("strategy"), row.get("symbol"), str(row.get("ts", ""))[:10])
    hits = closed.get(key)
    return hits[0] if hits else None


def report(account: str = "sim") -> None:
    rows = _load(account)
    if not rows:
        return

    closed = _closed_trades(account)
    n_total = len(rows)
    n_exit_now = sum(1 for r in rows if r.get("action") == "EXIT_NOW")
    n_hold     = sum(1 for r in rows if r.get("action") == "HOLD")

    print(f"\n{B}Phase C Counterfactual Report{X}  "
          f"{DIM}({n_total} exit evaluations, account={account}){X}")
    print(f"\n  EXIT_NOW decisions: {n_exit_now}   HOLD decisions: {n_hold}")

    # ── Match EXIT_NOW decisions to closed P&L ────────────────────────────
    matched:   list[dict] = []
    unmatched: list[dict] = []

    for row in rows:
        if row.get("action") != "EXIT_NOW":
            continue
        actual = _match(row, closed)
        est    = float(row.get("est_pnl_eur_at_decision") or 0)
        if actual is None:
            unmatched.append(row)
            continue
        delta = est - actual   # positive = give-back saved
        matched.append({
            "ts":       str(row.get("ts", ""))[:10],
            "strategy": row.get("strategy", ""),
            "symbol":   row.get("symbol", ""),
            "r_now":    float(row.get("r_now") or 0),
            "mfe_r":    float(row.get("mfe_r") or 0),
            "giveback": float(row.get("giveback_frac") or 0),
            "adv_score":float(row.get("exit_advisor_score") or 0),
            "est":      est,
            "actual":   actual,
            "delta":    delta,
            "comment":  row.get("comment", ""),
        })

    if not matched:
        print(f"\n{Y}  No matched closed trades yet ({len(unmatched)} EXIT_NOW decisions "
              f"still open or unmatched). Re-check after more positions close.{X}")
        return

    net_delta    = sum(r["delta"] for r in matched)
    total_est    = sum(r["est"]    for r in matched)
    total_actual = sum(r["actual"] for r in matched)

    # ── 1. Headline ───────────────────────────────────────────────────────
    colour = G if net_delta > 0 else R
    print(f"\n{B}If EXIT_NOW decisions had been applied:{X}")
    print(f"\n  Net give-back saved:          {colour}{net_delta:>+8.1f} EUR{X}"
          f"  ({len(matched)} matched EXIT_NOW trades)")
    print(f"  P&L banked at EXIT_NOW:        {total_est:>+8.1f} EUR  (estimated)")
    print(f"  P&L at natural exit (actual):  {total_actual:>+8.1f} EUR")

    # ── 2. Good vs bad EXIT_NOW calls ────────────────────────────────────
    saved  = [r for r in matched if r["delta"] > 0]   # correctly called early exit
    missed = [r for r in matched if r["delta"] <= 0]  # would have left gains
    print(f"\n{B}Decision quality:{X}")
    print(f"  Good exits (saved give-back):  {len(saved):>3}  "
          f"{G}{sum(r['delta'] for r in saved):>+8.1f} EUR{X}")
    print(f"  Premature exits (left gains):  {len(missed):>3}  "
          f"{R}{sum(r['delta'] for r in missed):>+8.1f} EUR{X}")
    pct_good = 100 * len(saved) / len(matched) if matched else 0
    print(f"  Good call rate:  {G if pct_good >= 60 else R}{pct_good:.0f}%{X}"
          f"  {DIM}(target ≥60% before flipping shadow_mode){X}")

    # ── 3. Per-trade breakdown ────────────────────────────────────────────
    print(f"\n{B}EXIT_NOW trades (sorted by delta):{X}")
    print(f"  {DIM}{'date':<12} {'strat':<14} {'sym':<10} {'r_now':>5} {'mfe_r':>5} "
          f"{'giveback':>8} {'est':>7} {'actual':>7} {'delta':>7}{X}")
    for r in sorted(matched, key=lambda x: -x["delta"])[:20]:
        c = G if r["delta"] > 0 else R
        print(f"  {r['ts']:<12} {r['strategy']:<14} {r['symbol']:<10} "
              f"{r['r_now']:>5.1f} {r['mfe_r']:>5.1f} {r['giveback']:>8.0%} "
              f"{r['est']:>+7.1f} {r['actual']:>+7.1f} {c}{r['delta']:>+7.1f}{X}")

    # ── 4. By strategy ────────────────────────────────────────────────────
    by_strat: dict[str, list] = defaultdict(list)
    for r in matched:
        by_strat[r["strategy"]].append(r["delta"])
    if len(by_strat) > 1:
        print(f"\n{B}Net give-back saved by strategy:{X}")
        for strat, deltas in sorted(by_strat.items(), key=lambda x: -sum(x[1])):
            total = sum(deltas)
            c = G if total > 0 else R
            print(f"  {strat:<20}  {len(deltas):>3} exits  {c}{total:>+8.1f} EUR{X}")

    # ── 5. Avg give-back fraction at exit call ────────────────────────────
    avg_giveback = sum(r["giveback"] for r in matched) / len(matched)
    avg_r_now    = sum(r["r_now"]    for r in matched) / len(matched)
    avg_adv      = sum(r["adv_score"] for r in matched) / len(matched)
    print(f"\n{B}Context at EXIT_NOW call:{X}")
    print(f"  Avg r_now at call:          {avg_r_now:.2f}R")
    print(f"  Avg mfe_r at call:          {sum(r['mfe_r'] for r in matched)/len(matched):.2f}R")
    print(f"  Avg giveback_frac at call:  {avg_giveback:.0%}")
    print(f"  Avg exit_advisor score:     {avg_adv:.1f}")

    # ── 6. Pending (still open) ───────────────────────────────────────────
    if unmatched:
        print(f"\n  {DIM}{len(unmatched)} EXIT_NOW decisions still open / unmatched — "
              f"P&L not yet resolved.{X}")

    # ── 7. Phase C verdict ────────────────────────────────────────────────
    MIN_TRADES = 10
    print(f"\n{B}{'─'*60}{X}")
    if len(matched) < MIN_TRADES:
        print(f"{Y}  INSUFFICIENT DATA: {len(matched)} matched EXIT_NOW trades "
              f"(need {MIN_TRADES}+). Re-check after 3 weeks of Phase B.{X}")
    elif net_delta > 0 and pct_good >= 60:
        print(f"{G}  POSITIVE: Exit Copilot would have saved {net_delta:+.1f} EUR "
              f"give-back ({pct_good:.0f}% good-call rate).{X}")
        print(f"{G}  → Phase C flip candidate. Review premature exits above before deciding.{X}")
    elif net_delta > 0:
        print(f"{Y}  MARGINAL: net positive ({net_delta:+.1f} EUR) but good-call rate "
              f"{pct_good:.0f}% < 60%. More data needed.{X}")
    else:
        print(f"{R}  NEGATIVE: Exit Copilot calling exits too early "
              f"({net_delta:+.1f} EUR net). Do NOT flip yet.{X}")
    print()


if __name__ == "__main__":
    account = sys.argv[1] if len(sys.argv) > 1 else "sim"
    report(account)
