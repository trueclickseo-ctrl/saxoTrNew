"""
sweep_orphan_observation_cards_2026-09-02.py  --  ONE-TIME, idempotent.

2026-09-02: the crash-state re-entry bug (fixed 2026-09-01 by `1c00c12` +
`dedup_stacked_reentries_2026-09-01.py`) left ~85+ observation ENTRY cards
with no matching exit card and no matching open position -- each was a
buggy re-entry whose "close" was never processed by _run_exits(), so the
outcome is unknowable.

report_giveback.py and ai/features/trade_journal.py are exit-driven, so
these already contribute nothing to the stats -- but anything that
enumerates ENTRY cards (per-pair signal counts, "how many trades" reads)
miscounts them, and the data gap deserves an explicit marker rather than
silence.

This tags each such entry card with:
    "orphaned": true
    "orphan_reason": "<ledger exit_reason, or no_close_recorded>"
    "orphan_swept": "2026-09-02"

Idempotent: re-running skips already-tagged cards and re-checks the rest
(a card can stop being an orphan if its position is still open / gets a
real exit later). Backs up the file first.

Usage:  python sweep_orphan_observation_cards_2026-09-02.py [--apply]
        (dry-run without --apply)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
CARDS = os.path.join(BASE, "data", "trade_observation_cards.jsonl")
LEDGER = os.path.join(BASE, "data", "pnl_ledger.db")
STATE_FILES = [
    os.path.join(BASE, "data", "forex_state.json"),
    os.path.join(BASE, "data", "forex_live_state.json"),
    os.path.join(BASE, "data", "forex_live_eur_state.json"),
]
SWEEP_TAG = "2026-09-02"


def _open_card_ids() -> set:
    ids: set = set()
    for f in STATE_FILES:
        try:
            with open(f, encoding="utf-8") as fh:
                st = json.load(fh)
        except Exception:
            continue
        for v in st.get("positions", {}).values():
            cid = v.get("observation_card_id")
            if cid:
                ids.add(cid)
    return ids


def _ledger_exit_reason_by_key() -> dict:
    """(strategy, symbol) -> most recent closed exit_reason, best-effort."""
    out: dict = {}
    if not os.path.exists(LEDGER):
        return out
    try:
        con = sqlite3.connect(LEDGER)
        for strat, sym, reason in con.execute(
            "SELECT strategy, symbol, exit_reason FROM trades "
            "WHERE module='forex' AND status='closed' ORDER BY id"
        ):
            out[(strat, sym)] = reason
        con.close()
    except Exception:
        pass
    return out


def main(apply: bool) -> int:
    if not os.path.exists(CARDS):
        print(f"no {CARDS}")
        return 1

    cards = []
    with open(CARDS, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                try:
                    cards.append(json.loads(ln))
                except Exception:
                    pass

    exit_ids = {c["card_id"] for c in cards if c.get("event") == "exit" and c.get("card_id")}
    open_ids = _open_card_ids()
    ledger_reason = _ledger_exit_reason_by_key()

    already = sum(1 for c in cards if c.get("event") == "entry" and c.get("orphaned"))
    tagged = 0
    by_strat: Counter = Counter()
    for c in cards:
        if c.get("event") != "entry" or not c.get("card_id"):
            continue
        if c.get("orphaned"):
            continue  # already swept
        cid = c["card_id"]
        if cid in exit_ids or cid in open_ids:
            continue  # has a real close, or still open -- not an orphan
        reason = ledger_reason.get((c.get("strategy"), c.get("symbol"))) or "no_close_recorded"
        c["orphaned"] = True
        c["orphan_reason"] = reason
        c["orphan_swept"] = SWEEP_TAG
        tagged += 1
        by_strat[f"{c.get('account_env')}:{c.get('strategy')}"] += 1

    print(f"entry cards total  : {sum(1 for c in cards if c.get('event') == 'entry')}")
    print(f"already tagged      : {already}")
    print(f"newly orphaned      : {tagged}")
    for k, n in by_strat.most_common():
        print(f"    {k:28} {n}")

    if not apply:
        print("\n(dry run -- pass --apply to write)")
        return 0
    if tagged == 0:
        print("\nnothing to write")
        return 0

    bak = CARDS + f".bak_orphan_sweep_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}"
    shutil.copy2(CARDS, bak)
    tmp = CARDS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for c in cards:
            fh.write(json.dumps(c, default=str) + "\n")
    os.replace(tmp, CARDS)
    print(f"\nbackup: {bak}")
    print(f"wrote {len(cards)} cards, {tagged} newly tagged orphaned")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
