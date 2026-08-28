"""
Regression test -- 2026-08-28 futures phantom-position-on-rejected-entry bug.

Real incident: user received an email "Strategy: RSI, Signal: Buy 3x GC,
... Order ID: None" then couldn't find the position in their open
positions. Traced to futures_scheduler.log: Saxo genuinely REJECTED the
GC entry order (400 Bad Request) -- saxo_order._place_entry_then_stop()
caught that internally and returned (None, None) per its own documented
contract ("Callers MUST check for a None entry_oid and skip recording a
position — nothing was actually opened at the broker"). forex/runner.py
already implements that check correctly; futures/runner.py never did --
it fell straight through to logging "Buy None: 3x GC[LONG]...", emailing
a misleading trade-opened alert, and writing a phantom "HOLD" position
into local state for a trade that never existed at the broker. The
phantom state entry was self-correcting (housekeeping's orphan-detection
removed it within the same run cycle, confirmed in the log: "futures/
removed_orphan GC: rsi:GC: local Buy 3 has no live backing at all"), but
the misleading email and log line were real.

This test uses source-inspection (not a live import) to avoid a real,
transient conflict: futures/runner.py's module-level logging.FileHandler
opens today's log file exclusively, which fails with PermissionError
whenever a real scheduled futures task happens to be running at the same
moment -- confirmed live while writing this fix.
"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        result = fn()
        if result is None:
            result = True
        _results.append((name, bool(result), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


def _futures_runner_source():
    with open(os.path.join(BASE_DIR, "futures", "runner.py"), encoding="utf-8") as f:
        return f.read()


def _forex_runner_source():
    with open(os.path.join(BASE_DIR, "forex", "runner.py"), encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════════════
section("1. futures/runner.py now checks for a None entry_oid before recording a position")
# ═══════════════════════════════════════════════════════════════════════

def test_futures_runner_checks_oid_is_none():
    src = _futures_runner_source()
    assert "if oid is None:" in src, (
        "expected futures/runner.py to check `if oid is None:` after "
        "saxo_order.place_with_stop() returns, mirroring forex/runner.py's "
        "entry_oid is None check"
    )
_run("futures/runner.py has an `if oid is None:` guard after place_with_stop()",
     test_futures_runner_checks_oid_is_none)


def test_futures_runner_skips_recording_on_none_oid():
    src = _futures_runner_source()
    # The guard must appear BEFORE the positions[...] assignment and
    # _send_trade_alert() call, and must `continue` rather than fall
    # through -- structurally verify by checking the None-check block
    # contains a `continue` and precedes the position-recording dict.
    none_check_idx = src.find("if oid is None:")
    positions_assign_idx = src.find('positions[f"{strat_name}:{sym}"] = {')
    send_alert_idx = src.find("_send_trade_alert(strat_name, direction, sym, qty,")
    assert none_check_idx != -1 and positions_assign_idx != -1 and send_alert_idx != -1
    assert none_check_idx < positions_assign_idx, (
        "the None-oid check must come BEFORE the position gets recorded in state"
    )
    assert none_check_idx < send_alert_idx, (
        "the None-oid check must come BEFORE the trade-opened email alert is sent"
    )
    # The guard block itself must contain a `continue`
    guard_block = src[none_check_idx:positions_assign_idx]
    assert "continue" in guard_block, (
        "expected the None-oid guard to `continue` (skip this signal), not just log"
    )
_run("The None-oid guard runs BEFORE both position-recording and the email alert, and uses `continue`",
     test_futures_runner_skips_recording_on_none_oid)


def test_futures_matches_forex_pattern():
    # Both modules should follow the same documented contract from
    # saxo_order._place_entry_then_stop()'s docstring.
    futures_src = _futures_runner_source()
    forex_src = _forex_runner_source()
    assert "entry_oid is None" in forex_src, "expected forex/runner.py's existing None-check (the reference pattern) to still be present"
    assert "oid is None" in futures_src, "expected futures/runner.py to have its own equivalent None-check now"
_run("futures/runner.py's fix mirrors forex/runner.py's pre-existing, correct pattern",
     test_futures_matches_forex_pattern)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {name}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
