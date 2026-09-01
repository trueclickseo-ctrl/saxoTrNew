"""
One-time cleanup -- 2026-09-01 duplicate 'open' rows in data/pnl_ledger.db.

pnl_tracker.log_open used to append a fresh row every scan a strategy
re-signalled a pair it already held, without the prior row being closed
(the duplicate-open-row bug; forex's sync path was fixed 2026-08-21 but
log_open itself kept doing it through ~2026-08-27, and etf far longer).
verify_ai_data.py flags 165 (module, strategy, symbol) groups with >1
open row -- 1732 excess rows, almost all forex SIM (719 vs 153 real state
positions) and etf (1349).

Impact: get_strategy_summary() already filters status='closed' so the
per-strategy P&L / win-rate reports are unaffected. But pnl_tracker.
log_close() does `... status='open' LIMIT 1` with no ORDER BY -> it can
close a STALE row instead of the real current one, corrupting P&L going
forward. And any open-exposure view is inflated.

Fix: per group, keep the newest (highest-id) open row, mark the rest
closed with exit_reason='ledger_dedup_2026-09-01', realized_pnl=NULL
(so every SUM/AVG ignores them). The LIVE accounts (forex_live,
forex_live_eur, futures) are already clean; forex_live_eur has exactly
one dup (GBPUSD 1820+2047) which this also fixes.

    python dedup_ledger_open_rows_2026-09-01.py            # dry run
    python dedup_ledger_open_rows_2026-09-01.py --apply
"""
import argparse
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(BASE, "data", "pnl_ledger.db")
G, R, Y, DIM, X, B = "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m", "\033[1m"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if not os.path.exists(LEDGER):
        print("no ledger")
        return 1

    con = sqlite3.connect(LEDGER)
    con.row_factory = sqlite3.Row
    groups = defaultdict(list)
    for r in con.execute("SELECT id, module, strategy, symbol, timestamp_open "
                         "FROM trades WHERE status!='closed' ORDER BY id"):
        groups[(r["module"], r["strategy"], r["symbol"])].append(r["id"])

    dups = {k: v for k, v in groups.items() if len(v) > 1}
    close_ids = []
    for (mod, strat, sym), ids in sorted(dups.items()):
        keep = max(ids)
        close_ids += [i for i in ids if i != keep]

    by_mod = defaultdict(int)
    for (mod, _, _), ids in dups.items():
        by_mod[mod] += len(ids) - 1
    print(f"{len(dups)} groups with duplicate open rows | {len(close_ids)} rows to close\n")
    for mod, n in sorted(by_mod.items()):
        print(f"  {mod:16s} {n:5d} excess open rows -> closed (newest kept)")
    print(f"\n  sample groups:")
    for (mod, strat, sym), ids in list(sorted(dups.items(), key=lambda kv: -len(kv[1])))[:8]:
        print(f"  {DIM}{mod}/{strat}/{sym}{X}: {len(ids)} open -> keep {max(ids)}, close {len(ids)-1}")

    if not a.apply:
        print(f"\n{Y}DRY RUN — re-run with --apply to write.{X}")
        con.close()
        return 0

    bak = LEDGER + ".bak_dedup_2026-09-01"
    shutil.copy(LEDGER, bak)
    now = datetime.now().isoformat()
    con.executemany(
        "UPDATE trades SET status='closed', realized_pnl=NULL, exit_price=NULL, "
        "exit_reason='ledger_dedup_2026-09-01', timestamp_close=? WHERE id=?",
        [(now, i) for i in close_ids],
    )
    con.commit()
    con.close()
    print(f"\n{G}APPLIED — closed {len(close_ids)} stale open rows. Backup: {os.path.basename(bak)}{X}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
