"""report_phase_b_counterfactual.py — Phase B gate evidence.

Quantifies the net EUR effect if the Copilot's shadow decisions had been
applied to the SIM book since the shadow study began.

Joins data/ai_shadow_decisions.jsonl against pnl_ledger.db and asks:

  "If every REJECT had been skipped, and every MODIFY had been traded at
   the agent's multiplier, what would the net P&L change have been?"

  REJECT entered = we entered despite the REJECT shadow. Applying it would
                   have saved the loss (or foregone the gain).
  MODIFY entered = we entered at full size. Applying would have scaled the
                   P&L by (multiplier − 1) — negative = saved loss on a
                   loser, positive = reduced gain on a winner.
  APPROVE        = baseline, no counterfactual change.

This is the last gate before flipping shadow_mode → false.

Usage:
    python report_phase_b_counterfactual.py              # sim (default)
    python report_phase_b_counterfactual.py sim
    python report_phase_b_counterfactual.py ai_sim
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict

BASE = os.path.dirname(os.path.abspath(__file__))
DECISIONS = os.path.join(BASE, "data", "ai_shadow_decisions.jsonl")
LEDGER    = os.path.join(BASE, "data", "pnl_ledger.db")

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)

_MODULE_MAP = {
    "sim":        "forex",
    "live":       "forex_live",
    "live_eur":   "forex_live_eur",
    "ai_sim":     "forex_ai",
    "live_stocks":"stock_live",
}


def _load_decisions(account: str) -> list[dict]:
    if not os.path.exists(DECISIONS):
        print(f"{Y}No shadow decisions yet — data/ai_shadow_decisions.jsonl missing.{X}")
        return []
    out = []
    with open(DECISIONS, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if row.get("account_env") == account:
                out.append(row)
    return out


def _closed_trades(account: str) -> dict:
    """{(strategy, symbol, date): [(realized_pnl, ts_close), ...]}"""
    if not os.path.exists(LEDGER):
        return {}
    module = _MODULE_MAP.get(account, "forex")
    con = sqlite3.connect(LEDGER)
    rows = con.execute(
        "SELECT strategy, symbol, realized_pnl, timestamp_open, timestamp_close "
        "FROM trades WHERE module=? AND exit_price IS NOT NULL",
        (module,),
    ).fetchall()
    con.close()
    by_key: dict = defaultdict(list)
    for strat, sym, pnl, t_open, t_close in rows:
        d = (t_open or "")[:10]
        by_key[(strat, sym, d)].append((float(pnl or 0), t_close))
    return by_key


def _match_pnl(dec: dict, closed: dict) -> float | None:
    key = (dec.get("strategy"), dec.get("symbol"), (dec.get("ts") or "")[:10])
    hits = closed.get(key)
    return hits[0][0] if hits else None


def _pct_colour(val: float) -> str:
    return G if val > 0 else (R if val < 0 else DIM)


def report(account: str = "sim") -> None:
    decs = _load_decisions(account)
    if not decs:
        return

    closed = _closed_trades(account)
    n_total = len(decs)

    # ── Match decisions to closed trades ─────────────────────────────────
    # Each row: action, multiplier, entered, pnl, strategy, regime, symbol, ts
    matched: list[dict] = []
    unmatched_entered:  list[dict] = []   # REJECT/MODIFY entered, no closed P&L yet
    unmatched_approved: int = 0

    for dec in decs:
        action     = dec.get("agent_action", "HOLD")
        entered    = bool(dec.get("entered_by_atos"))
        multiplier = float(dec.get("agent_size_multiplier") or 1.0)
        pnl        = _match_pnl(dec, closed)

        if action not in ("APPROVE", "REJECT", "MODIFY"):
            continue

        if pnl is None:
            if action == "APPROVE":
                unmatched_approved += 1
            elif entered:
                unmatched_entered.append(dec)
            continue

        if action == "REJECT" and entered:
            # Counterfactual: would have skipped → delta = −pnl
            delta = -pnl
        elif action == "MODIFY" and entered:
            # Counterfactual: would have traded at multiplier fraction → delta = pnl × (m − 1)
            m = min(max(multiplier, 0.0), 1.0)
            delta = pnl * (m - 1.0)
        else:
            # APPROVE, or REJECT/MODIFY that ATOS also didn't enter → no delta
            delta = 0.0

        matched.append({
            "action":     action,
            "entered":    entered,
            "multiplier": multiplier,
            "pnl":        pnl,
            "delta":      delta,
            "strategy":   dec.get("strategy", ""),
            "regime":     dec.get("regime", ""),
            "symbol":     dec.get("symbol", ""),
            "ts":         (dec.get("ts") or "")[:10],
            "comment":    (dec.get("agent_comment") or "").strip(),
        })

    actionable = [r for r in matched if r["action"] in ("REJECT", "MODIFY") and r["entered"]]
    net_delta  = sum(r["delta"] for r in actionable)

    print(f"\n{B}Phase B Counterfactual Report{X}  "
          f"{DIM}({n_total} decisions, account={account}){X}")
    print(f"\n{B}If the Copilot had been applied (shadow_mode=false):{X}")

    # ── 1. Headline ───────────────────────────────────────────────────────
    matched_n   = len(actionable)
    baseline_pnl = sum(r["pnl"] for r in actionable)
    applied_pnl  = baseline_pnl + net_delta
    colour = _pct_colour(net_delta)
    print(f"\n  Net counterfactual effect:   {colour}{net_delta:>+8.1f} EUR{X}"
          f"  ({matched_n} actionable closed trades matched)")
    print(f"  Baseline P&L  (as traded):  {baseline_pnl:>+8.1f} EUR")
    print(f"  Counterfactual P&L:         {applied_pnl:>+8.1f} EUR")

    # ── 2. REJECT breakdown ───────────────────────────────────────────────
    rejects = [r for r in actionable if r["action"] == "REJECT"]
    if rejects:
        saved_losses = [r for r in rejects if r["pnl"] < 0]   # good blocks
        foregone_gains = [r for r in rejects if r["pnl"] > 0]  # bad blocks
        print(f"\n{B}REJECT decisions ({len(rejects)} entered despite REJECT):{X}")
        print(f"  Losses avoided (good blocks):   {len(saved_losses):>3}  "
              f"{G}{sum(-r['pnl'] for r in saved_losses):>+8.1f} EUR saved{X}")
        print(f"  Gains foregone (bad blocks):    {len(foregone_gains):>3}  "
              f"{R}{sum(-r['pnl'] for r in foregone_gains):>+8.1f} EUR missed{X}")
        print(f"  Net REJECT effect:              "
              f"{_pct_colour(sum(r['delta'] for r in rejects))}"
              f"{sum(r['delta'] for r in rejects):>+8.1f} EUR{X}")
        print(f"\n  {DIM}Top 10 REJECT trades (sorted by delta):{X}")
        for r in sorted(rejects, key=lambda x: x["delta"], reverse=True)[:10]:
            c = G if r["delta"] > 0 else R
            print(f"  {r['ts']}  {r['strategy']:<14} {r['symbol']:<10}  "
                  f"pnl {r['pnl']:>+7.1f}  delta {c}{r['delta']:>+7.1f}{X}")

    # ── 3. MODIFY breakdown ───────────────────────────────────────────────
    modifies = [r for r in actionable if r["action"] == "MODIFY"]
    if modifies:
        avg_m = sum(r["multiplier"] for r in modifies) / len(modifies)
        print(f"\n{B}MODIFY decisions ({len(modifies)} entered at full size):{X}")
        print(f"  Avg multiplier the agent wanted:  {avg_m:.2f}x")
        net_mod = sum(r["delta"] for r in modifies)
        print(f"  Net MODIFY effect:               "
              f"{_pct_colour(net_mod)}{net_mod:>+8.1f} EUR{X}")
        losers = [r for r in modifies if r["pnl"] < 0]
        winners = [r for r in modifies if r["pnl"] > 0]
        if losers:
            print(f"  On losing trades  ({len(losers)}):  "
                  f"{G}{sum(r['delta'] for r in losers):>+7.1f} EUR saved (smaller size){X}")
        if winners:
            print(f"  On winning trades ({len(winners)}):  "
                  f"{R}{sum(r['delta'] for r in winners):>+7.1f} EUR reduced (smaller size){X}")

    # ── 4. By strategy ────────────────────────────────────────────────────
    by_strat: dict[str, list] = defaultdict(list)
    for r in actionable:
        by_strat[r["strategy"]].append(r["delta"])
    if by_strat:
        print(f"\n{B}Net counterfactual effect by strategy:{X}")
        for strat, deltas in sorted(by_strat.items(), key=lambda x: -sum(x[1])):
            total = sum(deltas)
            c = _pct_colour(total)
            print(f"  {strat:<20}  {len(deltas):>3} trades  {c}{total:>+8.1f} EUR{X}")

    # ── 5. By regime ──────────────────────────────────────────────────────
    by_regime: dict[str, list] = defaultdict(list)
    for r in actionable:
        by_regime[r["regime"] or "UNKNOWN"].append(r["delta"])
    if by_regime:
        print(f"\n{B}Net counterfactual effect by regime:{X}")
        for regime, deltas in sorted(by_regime.items(), key=lambda x: -sum(x[1])):
            total = sum(deltas)
            c = _pct_colour(total)
            print(f"  {regime:<24}  {len(deltas):>3} trades  {c}{total:>+8.1f} EUR{X}")

    # ── 6. Unmatched (still open or not entered) ──────────────────────────
    print(f"\n{B}Coverage:{X}")
    print(f"  Actionable decisions matched to closed trades: {len(actionable)}")
    print(f"  Still open / no P&L yet:                       {len(unmatched_entered)}")
    print(f"  APPROVE decisions (baseline, no delta):         {unmatched_approved + len([r for r in matched if r['action']=='APPROVE'])}")
    if unmatched_entered:
        syms = ", ".join(sorted({d.get("symbol","") for d in unmatched_entered[:8]}))
        print(f"  {DIM}Pending: {syms}{'...' if len(unmatched_entered) > 8 else ''}{X}")

    # ── 7. Phase B verdict ────────────────────────────────────────────────
    print(f"\n{B}{'─'*60}{X}")
    MIN_TRADES = 10
    if len(actionable) < MIN_TRADES:
        print(f"{Y}  INSUFFICIENT DATA: {len(actionable)} actionable closed trades "
              f"(need {MIN_TRADES}+). Re-check 2026-09-13.{X}")
    elif net_delta > 0:
        print(f"{G}  POSITIVE: applying the Copilot would have added "
              f"{net_delta:+.1f} EUR over this window.{X}")
        print(f"{G}  → Phase B flip candidate. Review the trade-list above before deciding.{X}")
    else:
        print(f"{R}  NEGATIVE: applying the Copilot would have cost "
              f"{net_delta:+.1f} EUR over this window.{X}")
        print(f"{R}  → Do NOT flip shadow_mode yet. Investigate top bad blocks above.{X}")
    print()


if __name__ == "__main__":
    account = sys.argv[1] if len(sys.argv) > 1 else "sim"
    report(account)
