"""
fix_observation_card_mae_mfe.py -- one-time repair (2026-09-01).

The forward-observation MAE/MFE was measured over the FULL ~350-bar daily
chart window instead of the trade's holding period (forex/runner.py
`_run_exits`, `since_entry = df`). On trending / volatile crosses this
inflated the "worst unrealised excursion" to tens of thousands of EUR
against a ~EUR80 risk -- 67 of 68 closed trades in
data/trade_observation_cards.jsonl carried a corrupted mae_eur/mfe_eur
(the AI Trading Journal spotted it: "MAE -9412 EUR vs 76 EUR risk").

Those numbers cannot be recomputed accurately after the fact (no intrabar
history for past trades, and the daily windows have since moved), so this
script NULLS mae_eur / mfe_eur on every historical exit card and stamps
`mae_mfe_invalidated`. Clean MAE/MFE accrues from the runner fix forward.

Read-only w.r.t. all trading state -- only rewrites the observation-card
log (and a .bak beside it). Nothing here touches an order, position, or
strategy.

    python fix_observation_card_mae_mfe.py          # dry run -- show what would change
    python fix_observation_card_mae_mfe.py --apply  # rewrite the file (after a .bak)
"""

import json
import os
import shutil
import sys
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CARDS = os.path.join(BASE, "data", "trade_observation_cards.jsonl")
REASON = "unbounded-daily-window-bug-2026-09-01"


def main() -> int:
    apply = "--apply" in sys.argv
    if not os.path.exists(CARDS):
        print(f"no {CARDS}")
        return 0

    rows = []
    for ln in open(CARDS, encoding="utf-8"):
        ln = ln.strip()
        if ln:
            try:
                rows.append(json.loads(ln))
            except Exception:
                pass

    touched = 0
    already = 0
    out = []
    for r in rows:
        if r.get("event") == "exit" and (r.get("mae_eur") is not None or r.get("mfe_eur") is not None):
            if r.get("mae_mfe_invalidated"):
                already += 1
            else:
                r = {**r,
                     "mae_eur_raw": r.get("mae_eur"), "mfe_eur_raw": r.get("mfe_eur"),
                     "mae_eur": None, "mfe_eur": None,
                     "mae_mfe_invalidated": REASON}
                touched += 1
        out.append(r)

    print(f"{len(rows)} card rows | {touched} exit card(s) to invalidate "
          f"| {already} already invalidated")
    if not apply:
        print("\ndry run -- re-run with --apply to rewrite "
              f"{os.path.relpath(CARDS, BASE)} (a .bak is made first)")
        return 0
    if not touched:
        print("nothing to do")
        return 0

    bak = CARDS + f".bak_{datetime.now():%Y-%m-%d_%H%M%S}_pre_mae_fix"
    shutil.copy2(CARDS, bak)
    tmp = CARDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, default=str) + "\n")
    os.replace(tmp, CARDS)
    print(f"rewrote {os.path.relpath(CARDS, BASE)} ({touched} invalidated)  ·  backup: {os.path.basename(bak)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
