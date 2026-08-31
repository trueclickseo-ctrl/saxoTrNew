"""
report_exit_advisor.py -- read-only scorecard for the Stage-A exit advisor.

The advisor (forex/exit_advisor.py) runs in SHADOW MODE: every exits-check
cycle it logs a HOLD / TIGHTEN / EXIT recommendation per open position to
data/exit_advisor_shadow.jsonl, and never acts. This script joins that log
against the real exit outcome (data/trade_observation_cards.jsonl) and asks
the only question that matters:

  For trades where the advisor said EXIT at some point, would exiting THEN
  (at that cycle's r_now) have beaten the trade's real exit R?

    edge_R = advisor_exit_r  -  actual_exit_r         (per trade)
    > 0  -> advisor would have kept more of the move
    < 0  -> advisor would have clipped a winner

Nothing here mutates state. Run any time:
    python report_exit_advisor.py
"""

import json
import os
import statistics
import sys
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
SHADOW = os.path.join(BASE, "data", "exit_advisor_shadow.jsonl")
CARDS  = os.path.join(BASE, "data", "trade_observation_cards.jsonl")

G, R, Y, C, DIM, X, B = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[2m", "\033[0m", "\033[1m"
)


def _jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    for ln in open(path, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def main():
    shadow = _jsonl(SHADOW)
    cards  = _jsonl(CARDS)
    if not shadow:
        print(f"{Y}No shadow rows yet (data/exit_advisor_shadow.jsonl). "
              f"The advisor logs one row per open position per exits-check "
              f"cycle once forex/runner.py has run with EXIT_ADVISOR_MODE='shadow'.{X}")
        return 0

    # closed trades keyed by card_id
    entry = {c["card_id"]: c for c in cards if c.get("event") == "entry"}
    closed = {}
    for c in cards:
        if c.get("event") == "exit" and c["card_id"] in entry:
            closed[c["card_id"]] = {**entry[c["card_id"]], **c}

    # shadow rows grouped by card_id, in time order
    by_card = {}
    for s in shadow:
        by_card.setdefault(s.get("card_id"), []).append(s)
    for v in by_card.values():
        v.sort(key=lambda r: r.get("timestamp", ""))

    rec_counts = Counter(s["recommendation"] for s in shadow)
    print(f"{B}Exit advisor -- shadow scorecard{X}  {DIM}({len(shadow)} cycle-observations, "
          f"{len(closed)} closed trades){X}")
    print(f"  recommendation mix across all cycles: {dict(rec_counts)}")

    edges, clipped, saved, no_signal = [], 0, 0, 0
    rows = []
    for cid, trade in closed.items():
        actual_r = trade.get("r_multiple")
        if actual_r is None:
            continue
        sh = by_card.get(cid, [])
        first_exit = next((s for s in sh if s["recommendation"] == "EXIT"), None)
        if first_exit is None:
            no_signal += 1
            continue
        adv_r = first_exit["r_now"]
        edge = adv_r - actual_r
        edges.append(edge)
        if edge > 0.05:
            saved += 1
        elif edge < -0.05:
            clipped += 1
        rows.append((trade.get("strategy", "?"), trade.get("symbol", "?"),
                     first_exit.get("score"), adv_r, actual_r, edge,
                     (trade.get("exit_reason") or "").split(" ")[0]))

    print(f"\n  {B}closed trades where the advisor said EXIT at some point: {len(edges)}{X}")
    print(f"    would have kept more (edge > 0): {G}{saved}{X}   "
          f"would have clipped a winner (edge < 0): {R}{clipped}{X}")
    if edges:
        print(f"    mean edge R: {statistics.mean(edges):+.2f}   median: {statistics.median(edges):+.2f}   "
              f"total: {sum(edges):+.1f} R")
    print(f"  closed trades the advisor never flagged EXIT on: {no_signal}")

    if rows:
        print(f"\n  {DIM}{'strat':<22}{'sym':<9}{'score':>6}{'advR':>7}{'realR':>8}{'edgeR':>8}  exit{X}")
        for st, sy, sc, ar, rr, ed, er in sorted(rows, key=lambda r: r[5]):
            col = G if ed > 0 else (R if ed < 0 else "")
            print(f"  {st:<22}{sy:<9}{(sc if sc is not None else 0):>6.0f}"
                  f"{ar:>7.2f}{rr:>8.2f}{col}{ed:>+8.2f}{X}  {er}")

    n = len(edges)
    print(f"\n{B}{'-'*60}{X}")
    if n < 25:
        print(f"{Y}  {n} decision(s) so far -- not enough. Re-run weekly; "
              f"~25-40+ EXIT-flagged trades before the mean edge is trustworthy.{X}")
    else:
        m = statistics.mean(edges)
        verd = (f"{G}advisor is ahead (+{m:.2f} R/trade) -- consider promoting to TIGHTEN-only 'active'"
                if m > 0.1 else
                f"{R}advisor is behind ({m:+.2f} R/trade) -- keep it in shadow, retune the score"
                if m < -0.1 else
                f"{Y}roughly neutral ({m:+.2f} R/trade) -- no clear win yet")
        print(f"  {verd}{X}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
