"""
dedup_stacked_reentries_2026-09-01.py -- one-time cleanup.

2026-09-01: a state/ledger race (fixed same day -- see forex/runner.py's
`state=` checkpointing in _run_entries/_run_exits, and the ledger-backed
open_syms guard) let several SIM strategies re-enter a pair they already
held, because a scan's entry was written to pnl_ledger.db immediately but
the state file (which `open_symbols` is built from) hadn't been
checkpointed yet when the next scan ran. Result: one `(strategy, symbol)`
tracked by ONE state key but several OPEN rows in the ledger.

For each (module, strategy, symbol) with more than one OPEN row:
  * if the CURRENT state file still holds that key -- the real, live
    position -- match it to the ledger row with the same direction/
    quantity/closest entry_price and keep THAT one open; close every
    other row in the group (status='closed', realized_pnl=NULL,
    exit_reason='dedup_stacked_reentry_2026-09-01' -- the true exit P&L
    for a phantom duplicate is unknown, so NULL, same convention as
    _close_orphan_ledger_rows).
  * if the state file no longer holds that key at all (the strategy has
    since exited and _close_orphan_ledger_rows() hasn't caught up yet),
    close every row in the group the same way.

Never fabricates a P&L. Never touches LIVE (`forex_live`/`forex_live_eur`
had zero duplicates when this was written). Backs up pnl_ledger.db before
writing. Read the printed report before re-running with --apply.

    python dedup_stacked_reentries_2026-09-01.py           # dry run
    python dedup_stacked_reentries_2026-09-01.py --apply
"""
import json
import os
import shutil
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "data", "pnl_ledger.db")
STATE_PATH = os.path.join(BASE, "data", "forex_state.json")
MODULE = "forex"          # SIM only -- see docstring
REASON = "dedup_stacked_reentry_2026-09-01"


def _state_positions() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f).get("positions", {})
    except Exception:
        return {}


def _best_match(rows: list[dict], want: dict | None) -> int | None:
    """Return the id of the row that matches `want` (state's current
    direction/quantity/entry_price), or None if `want` is None / no row
    is close. Ties broken by latest timestamp_open (state always reflects
    the MOST RECENT entry for that key)."""
    if not want:
        return None
    candidates = [r for r in rows if r["direction"] == want.get("direction")]
    if not candidates:
        candidates = rows
    def _score(r):
        qty_ok = abs(r["quantity"] - float(want.get("quantity", 0))) < 1e-6
        px = want.get("entry_price")
        px_diff = abs(r["entry_price"] - px) / px if px else 1.0
        return (0 if qty_ok else 1, px_diff)
    candidates.sort(key=_score)
    return candidates[0]["id"]


def plan() -> list[dict]:
    positions = _state_positions()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, strategy, symbol, direction, quantity, entry_price, timestamp_open "
        "FROM trades WHERE module=? AND status='open' ORDER BY strategy, symbol, id",
        (MODULE,)).fetchall()
    con.close()

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["strategy"], r["symbol"])].append(dict(r))

    actions = []
    for (strat, sym), grp in groups.items():
        if len(grp) < 2:
            continue
        want = positions.get(f"{strat}:{sym}")
        # state no longer holds this key at all -> nothing here is really
        # open; close every row (matches _close_orphan_ledger_rows'
        # convention for "no state position"), don't guess which was real.
        keep_id = _best_match(grp, want) if want else None
        for r in grp:
            actions.append({
                "id": r["id"], "strategy": strat, "symbol": sym,
                "direction": r["direction"], "quantity": r["quantity"],
                "entry_price": r["entry_price"], "timestamp_open": r["timestamp_open"],
                "keep": r["id"] == keep_id,
                "in_state": want is not None,
            })
    return actions


def main() -> int:
    apply = "--apply" in sys.argv
    actions = plan()
    if not actions:
        print("No stacked (module=forex) open rows found -- nothing to do.")
        return 0

    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for a in actions:
        by_key[(a["strategy"], a["symbol"])].append(a)

    to_close = [a["id"] for a in actions if not a["keep"]]
    print(f"{'APPLYING' if apply else 'DRY RUN'} -- {len(by_key)} stacked pair(s), "
          f"{len(actions)} rows, {len(to_close)} to close, {len(actions) - len(to_close)} kept open\n")
    for (strat, sym), rows in sorted(by_key.items()):
        in_state = rows[0]["in_state"]
        print(f"  {strat}:{sym}  ({len(rows)} open rows, "
              f"{'in current state' if in_state else 'NOT in current state -- close all'})")
        for r in rows:
            tag = "KEEP" if r["keep"] else "close"
            print(f"    [{tag}] id={r['id']:<6} {r['direction']:4} {r['quantity']:>8} "
                  f"@ {r['entry_price']}  opened {r['timestamp_open']}")

    if not apply:
        print("\nDry run only -- re-run with --apply to write these changes.")
        return 0

    bak = DB_PATH + f".bak_dedup-stacked-reentries_{datetime.now():%Y-%m-%d_%H%M%S}"
    shutil.copy2(DB_PATH, bak)
    print(f"\nBacked up {DB_PATH} -> {bak}")

    con = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()
    by_id = {a["id"]: a for a in actions}
    con.executemany(
        "UPDATE trades SET status='closed', realized_pnl=NULL, exit_reason=?, "
        "timestamp_close=? WHERE id=? AND status='open'",
        [(REASON if by_id[i]["in_state"] else "reconciled_no_state", now, i) for i in to_close])
    con.commit()
    con.close()
    print(f"Closed {len(to_close)} duplicate row(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
