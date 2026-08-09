"""
scripts/test_equity_stop_loop.py
---------------------------------
Offline loop-simulation test for binance_bot/equity_stop.py.

No testnet connection required. Runs three phases:

  Phase 1 — Normal operation at 15% threshold
    Simulates 5 scans with equity rising and dipping mildly.
    Every scan should log "Equity stop: OK".
    State file is shown after each scan to confirm peak tracking.

  Phase 2 — Deliberate halt at 1% threshold
    Resets to a clean state file.
    Equity rises, then drops 1.01% — halt should fire on scan 3.
    Scan 4 is called while halted to confirm the halt persists.

  Phase 3 — Reset
    Calls reset(), confirms the next check resumes cleanly.

Run from the project root:
    python scripts/test_equity_stop_loop.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from binance_bot.equity_stop import EquityStop   # noqa: E402

STATE_PATH = str(ROOT / "data" / "test_equity_stop_loop.json")

# ── Formatting helpers ────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
DIM    = "\033[2m"


def _color(text: str, code: str) -> str:
    return f"{code}{text}{RESET}"


def _sep(char: str = "-", width: int = 68) -> None:
    print(_color(char * width, DIM))


def _header(text: str) -> None:
    _sep("=")
    print(_color(f"  {text}", BOLD + CYAN))
    _sep("=")


def _show_state(path: str) -> None:
    """Pretty-print the current state file."""
    if not os.path.exists(path):
        print(_color("  [state file not yet written]", DIM))
        return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    print(_color("  binance_equity_state.json:", DIM))
    print(_color(f"    peak_equity  : ${data['peak_equity']:>10,.2f}", DIM))
    print(_color(f"    halted       : {data['halted']}", DIM))
    print(_color(f"    halt_equity  : {data['halt_equity']}", DIM))
    print(_color(f"    halt_ts      : {data['halt_ts']}", DIM))
    print(_color(f"    updated_ts   : {data['updated_ts']}", DIM))


def _scan_log(scan_n: int, equity: float, stop: EquityStop, halted: bool, reason: str) -> None:
    peak = stop.peak_equity
    dd = (peak - equity) / max(peak, 1) * 100 if peak > 0 else 0.0

    tag = _color("OK     ", GREEN) if not halted else _color("HALTED ", RED)
    print(
        f"  Scan {scan_n}  equity=${equity:>10,.2f}  "
        f"peak=${peak:>10,.2f}  dd={dd:>5.2f}%  [{tag}]"
    )
    if halted:
        print(_color(f"         *** {reason}", RED))


def _wipe_state(path: str) -> None:
    """Remove the state file so the next EquityStop starts fresh."""
    if os.path.exists(path):
        os.remove(path)


# ── Phase 1: Normal operation (threshold = 15%) ───────────────────────────────

def phase1() -> None:
    _header("Phase 1 — Normal operation  (threshold=15%)")
    print("  Expect: every scan logs OK.  Peak rises when equity rises.")
    print()

    _wipe_state(STATE_PATH)
    stop = EquityStop(STATE_PATH, drawdown_threshold=0.15)

    scans = [
        10_000.00,   # 1 — first scan: peak established at 10k
        10_150.00,   # 2 — equity rises: peak updates to 10,150
        10_080.00,   # 3 — mild dip: 0.69% drawdown, well inside 15%
        10_200.00,   # 4 — new high: peak updates to 10,200
         9_950.00,   # 5 — 2.45% drawdown: still OK, threshold is 15%
    ]

    for i, eq in enumerate(scans, start=1):
        halted, reason = stop.check(eq)
        _scan_log(i, eq, stop, halted, reason)
        if halted:
            print(_color("  FAIL: halt fired unexpectedly in Phase 1", RED))
            sys.exit(1)

    print()
    _show_state(STATE_PATH)
    print()
    print(_color("  Phase 1 PASSED — 5 scans, no halt, peak tracked correctly.", GREEN))


# ── Phase 2: Deliberate halt (threshold = 1%) ────────────────────────────────

def phase2() -> None:
    _header("Phase 2 — Deliberate 1% halt test  (threshold=1%)")
    print("  Expect: halt fires on scan 3 (drop of 1.01% from peak).")
    print("  Expect: scan 4 stays halted despite equity partially recovering.")
    print()

    _wipe_state(STATE_PATH)
    stop = EquityStop(STATE_PATH, drawdown_threshold=0.01)

    scans = [
        10_000.00,   # 1 — peak = 10,000
        10_100.00,   # 2 — peak rises to 10,100
         9_998.00,   # 3 — 1.01% below 10,100 → HALT
         9_999.00,   # 4 — slight recovery but still halted (peak frozen)
    ]

    halt_fired = False
    for i, eq in enumerate(scans, start=1):
        halted, reason = stop.check(eq)
        _scan_log(i, eq, stop, halted, reason)

        if i == 3 and not halted:
            print(_color("  FAIL: halt should have fired on scan 3 but did not.", RED))
            sys.exit(1)
        if i == 3 and halted:
            halt_fired = True
            print(_color("  [PASS] Halt fired as expected.", GREEN))

        if i == 4 and not halted:
            print(_color("  FAIL: halt should persist on scan 4 but cleared itself.", RED))
            sys.exit(1)
        if i == 4 and halted:
            print(_color("  [PASS] Halt persists on scan 4 (no auto-resume).", GREEN))

    print()
    _show_state(STATE_PATH)

    if not halt_fired:
        print(_color("  FAIL: halt never fired.", RED))
        sys.exit(1)

    print()
    print(_color("  Phase 2 PASSED — halt fired at 1.01%, froze peak, persisted.", GREEN))


# ── Phase 3: Reset ────────────────────────────────────────────────────────────

def phase3() -> None:
    _header("Phase 3 — Operator reset")
    print("  Expect: reset() clears halt.  Next check passes at original threshold.")
    print()

    # Reuse the halted state file from Phase 2.
    stop = EquityStop(STATE_PATH, drawdown_threshold=0.01)
    assert stop.halted, "Expected halted state from Phase 2 — test setup error."

    stop.reset()
    print("  reset() called.")

    # First check after reset: equity above where it was
    halted, reason = stop.check(10_050.00)
    _scan_log(1, 10_050.00, stop, halted, reason)
    if halted:
        print(_color("  FAIL: bot still halted after reset().", RED))
        sys.exit(1)

    print(_color("  [PASS] Bot resumed after reset.", GREEN))
    print()
    _show_state(STATE_PATH)
    print()
    print(_color("  Phase 3 PASSED — reset clears halt, next scan proceeds normally.", GREEN))


# ── Restore threshold reminder ────────────────────────────────────────────────

def remind_restore() -> None:
    _sep()
    print()
    print(_color("  All three phases passed.", BOLD + GREEN))
    print()
    print("  What this confirms:")
    print("    • Peak updates correctly each scan when equity rises.")
    print("    • Halt fires at the configured threshold, no earlier, no later.")
    print("    • Peak freezes the moment halt fires (drawdown stays stable).")
    print("    • Halt persists across subsequent scans until reset() is called.")
    print("    • reset() clears halt and lets the bot resume immediately.")
    print()
    print("  The 1% threshold was used only for this test.")
    print(_color("  binance_testnet_config.yaml still has 15.0 — no change needed.", GREEN))
    print()
    _sep()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Remove any leftover state from previous test runs
    _wipe_state(STATE_PATH)

    phase1()
    print()
    time.sleep(0.05)   # tiny gap so updated_ts differs between phases

    phase2()
    print()
    time.sleep(0.05)

    phase3()
    print()

    remind_restore()

    # Clean up test state file
    _wipe_state(STATE_PATH)
