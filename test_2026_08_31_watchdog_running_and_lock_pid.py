"""
Regression test — 2026-08-31.

Two fixes for one incident. A transient DNS/network blip to
gateway.saxobank.com (getaddrinfo failed -> every Saxo call eating its full
15 s timeout + retries) made the four 12:00/12:05 forex scheduled tasks
overrun their windows. Fallout:

  1. scheduler_watchdog.py alerted on all four with "returned error code
     267009 and <log> isn't fresh either" — but 267009 is
     SCHED_S_TASK_RUNNING, a *status*, not an error: the runs were still
     grinding through the backlog and finished fine. The watchdog now
     treats a task that is currently Running (or reports 267009 /
     0x800710E0 ERROR_TASK_ALREADY_RUNNING) as in-progress, not failed,
     and only escalates if it has been Running past RUNNING_HANG_CEILING_MIN.

  2. proc_lock.acquire() only ever checked the lock file's AGE, so when a
     holder (intraday_monitor, PID 6748) crashed without releasing
     forex_runner.lock, every subsequent forex run burned the full 15 min
     WAIT_TIMEOUT before proceeding — compounding the slowdown. acquire()
     now reads the PID recorded in the lock file and, if that process is
     gone, clears the lock immediately instead of waiting.
"""

import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


# ═══════════════════════════════════════════════════════════════════════
section("1. scheduler_watchdog: a currently-Running task is not a failure")
# ═══════════════════════════════════════════════════════════════════════

import scheduler_watchdog as w


def _check(monkey_info, log_mtime_dt):
    """Run _check_windows_task for the real 'Forex Intraday Scan' registry
    entry with _query_task_info / _log_mtime stubbed."""
    orig_q, orig_m = w._query_task_info, w._log_mtime
    w._query_task_info = lambda tn: dict(monkey_info)
    w._log_mtime = lambda lf: log_mtime_dt
    try:
        task_name, log_file, grace, max_wait = w.WINDOWS_TASKS["Forex Intraday Scan"]
        return w._check_windows_task("Forex Intraday Scan", task_name, log_file, grace, max_wait)
    finally:
        w._query_task_info, w._log_mtime = orig_q, orig_m


def test_running_state_with_267009_and_stale_log_is_healthy():
    now = datetime.now()
    res = _check(
        {"last_run": now - timedelta(minutes=7), "next_run": now + timedelta(minutes=25),
         "last_result": w.TASK_CURRENTLY_RUNNING, "state": "Running"},
        # log last written well before this run started — exactly the
        # incident signature that used to trip the alert
        now - timedelta(minutes=40),
    )
    assert res is None, f"expected healthy for an in-progress run, got: {res!r}"
_run("Running + result 267009 + stale log -> healthy (was: false alarm)",
     test_running_state_with_267009_and_stale_log_is_healthy)


def test_already_running_hresult_is_healthy():
    now = datetime.now()
    res = _check(
        {"last_run": now - timedelta(minutes=3), "next_run": now + timedelta(minutes=27),
         "last_result": w.TASK_ALREADY_RUNNING, "state": "Ready"},
        now - timedelta(minutes=50),
    )
    assert res is None, f"0x800710E0 (ERROR_TASK_ALREADY_RUNNING) must be benign, got: {res!r}"
_run("result 0x800710E0 (task already running) -> healthy",
     test_already_running_hresult_is_healthy)


def test_running_past_hang_ceiling_escalates():
    now = datetime.now()
    res = _check(
        {"last_run": now - timedelta(minutes=w.RUNNING_HANG_CEILING_MIN + 20),
         "next_run": now + timedelta(minutes=10),
         "last_result": w.TASK_CURRENTLY_RUNNING, "state": "Running"},
        now - timedelta(hours=3),
    )
    assert res is not None and "hung" in res.lower(), (
        f"a task Running past the {w.RUNNING_HANG_CEILING_MIN}-min ceiling must still be flagged, got: {res!r}"
    )
_run("Running past RUNNING_HANG_CEILING_MIN -> still flagged as a hang",
     test_running_past_hang_ceiling_escalates)


def test_terminated_while_still_relaunching_is_healthy():
    now = datetime.now()
    # Intraday Monitor: 60 s cadence, 2 min kill limit. Terminated last
    # cycle (Saxo slow), relaunched 30 s ago — grace_min is 10 for this
    # registry entry, so last_run well within it.
    res = _check(
        {"last_run": now - timedelta(seconds=30), "next_run": now + timedelta(seconds=30),
         "last_result": w.TASK_TERMINATED_BY_SCHEDULER, "state": "Ready"},
        now - timedelta(minutes=5),  # log killed mid-write, a bit stale
    )
    assert res is None, f"a terminated run that's already relaunching must be benign, got: {res!r}"
_run("result 267014 (terminated) + task still relaunching on cadence -> healthy",
     test_terminated_while_still_relaunching_is_healthy)


def test_terminated_and_never_came_back_still_alerts():
    now = datetime.now()
    res = _check(
        {"last_run": now - timedelta(hours=2), "next_run": None,
         "last_result": w.TASK_TERMINATED_BY_SCHEDULER, "state": "Ready"},
        now - timedelta(hours=2, minutes=5),
    )
    assert res is not None, (
        f"a task terminated 2h ago with nothing since must still surface, got: {res!r}"
    )
_run("result 267014 but task went silent for hours -> still surfaces",
     test_terminated_and_never_came_back_still_alerts)


def test_genuine_error_code_still_alerts_when_log_stale():
    now = datetime.now()
    res = _check(
        {"last_run": now - timedelta(minutes=30), "next_run": now + timedelta(minutes=5),
         "last_result": 0x1,  # a real non-zero, non-running failure
         "state": "Ready"},
        now - timedelta(hours=2),  # stale log -> no escape hatch
    )
    assert res is not None and "error code" in res.lower(), (
        f"a real error code with a stale log must still alert, got: {res!r}"
    )
_run("A real error code (not a running-status code) with a stale log still alerts",
     test_genuine_error_code_still_alerts_when_log_stale)


def test_healthy_completed_run_still_healthy():
    now = datetime.now()
    res = _check(
        {"last_run": now - timedelta(minutes=10), "next_run": now + timedelta(minutes=20),
         "last_result": 0, "state": "Ready"},
        now - timedelta(minutes=6),  # fresh log, after last_run
    )
    assert res is None, f"a clean completed run must stay healthy, got: {res!r}"
_run("A clean completed run (result 0, fresh log) stays healthy",
     test_healthy_completed_run_still_healthy)


# ═══════════════════════════════════════════════════════════════════════
section("2. proc_lock: a lock whose holder PID is dead is stolen, not waited on")
# ═══════════════════════════════════════════════════════════════════════

import proc_lock


def test_pid_alive_self_true_bogus_false():
    assert proc_lock._pid_alive(os.getpid()) is True
    # A PID that is essentially guaranteed not to exist.
    assert proc_lock._pid_alive(999_999_991) is False
    # Conservative on nonsense input.
    assert proc_lock._pid_alive(0) is True
    assert proc_lock._pid_alive(-1) is True
_run("_pid_alive: True for self, False for a non-existent PID, conservative on junk",
     test_pid_alive_self_true_bogus_false)


def test_lock_holder_pid_parse():
    p = os.path.join(BASE_DIR, "data", "_test_proc_lock_parse.lock")
    with open(p, "w") as f:
        f.write("6748 intraday_monitor 2026-08-31T12:30:27.572988")
    try:
        assert proc_lock._lock_holder_pid(p) == 6748
    finally:
        os.remove(p)
    assert proc_lock._lock_holder_pid(p + ".nope") is None
_run("_lock_holder_pid parses the recorded PID, None when unreadable",
     test_lock_holder_pid_parse)


def test_acquire_steals_lock_from_dead_holder_without_waiting():
    lock = os.path.join(BASE_DIR, "data", "_test_proc_lock_dead_holder.lock")
    # Simulate a crashed holder: a dead PID, fresh mtime (well under
    # STALE_SECONDS, so the age check alone would make us wait).
    with open(lock, "w") as f:
        f.write("999999991 crashed-holder 2026-08-31T12:00:00")
    try:
        start = datetime.now()
        got = proc_lock.acquire(lock, "test-caller")
        elapsed = (datetime.now() - start).total_seconds()
        assert got is True, "acquire() should succeed"
        assert elapsed < 4, f"should not have polled/waited (took {elapsed:.1f}s)"
        assert proc_lock._lock_holder_pid(lock) == os.getpid(), "we should now own the lock"
    finally:
        proc_lock.release(lock)
        if os.path.exists(lock):
            os.remove(lock)
_run("acquire() clears a fresh lock held by a dead PID immediately (no 15-min wait)",
     test_acquire_steals_lock_from_dead_holder_without_waiting)


def test_acquire_still_waits_on_a_live_holder():
    lock = os.path.join(BASE_DIR, "data", "_test_proc_lock_live_holder.lock")
    # A live holder (this very process), fresh mtime.
    with open(lock, "w") as f:
        f.write(f"{os.getpid()} live-holder 2026-08-31T12:00:00")
    orig = proc_lock.WAIT_TIMEOUT
    proc_lock.WAIT_TIMEOUT = 6  # keep the test quick
    try:
        start = datetime.now()
        got = proc_lock.acquire(lock, "test-caller")
        elapsed = (datetime.now() - start).total_seconds()
        # Holder is alive -> must poll until the (shortened) timeout, then proceed.
        assert got is True
        assert elapsed >= 5, f"expected to wait out the timeout on a live holder (took {elapsed:.1f}s)"
    finally:
        proc_lock.WAIT_TIMEOUT = orig
        proc_lock.release(lock)
        if os.path.exists(lock):
            os.remove(lock)
_run("acquire() still waits out the timeout when the holder PID is alive",
     test_acquire_still_waits_on_a_live_holder)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
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
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
