"""
proc_lock.py — generic cross-process file lock for trading state files
--------------------------------------------------------------------------
Serializes any two OS processes that touch the same shared state file
(forex_state.json, futures_state.json, ...) at overlapping moments.

Found live 2026-08-24: forex_state.json's atomic write (a .tmp file +
os.replace) only prevents JSON corruption -- it does NOT protect against
two full processes racing to read-then-write it. Two independent
processes can both load state before either saves, both independently
decide to act on the same signal (e.g. open the same position, or one
closes a stop the other doesn't know about yet), and whichever saves
last silently overwrites the other's update. First found between two
forex/runner.py invocations sharing the exact same Task Scheduler
trigger time (ATOS Forex Gap Fill / Gap Monday Early, both Mon 03:00
PKT); then found that intraday_monitor.py -- a SEPARATE script/process
that independently reads and writes both forex_state.json and
futures_state.json -- was completely invisible to that first fix, since
it never goes through forex/runner.py's own CLI dispatch. This module
exists so every process/script that touches ANY shared trading-state
file uses the SAME lock semantics, regardless of which script or
scheduled task it is.

Usage:
    import proc_lock
    if proc_lock.acquire(proc_lock.FOREX_LOCK, "my-label"):
        try:
            ... touch forex_state.json ...
        finally:
            proc_lock.release(proc_lock.FOREX_LOCK)
"""

import os
import time
from datetime import datetime

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

FOREX_LOCK      = os.path.join(_DATA_DIR, "forex_runner.lock")
FUTURES_LOCK    = os.path.join(_DATA_DIR, "futures_runner.lock")
# 2026-08-25: the real-money LIVE forex account gets its OWN lock, separate
# from FOREX_LOCK. Found live: intraday_monitor.py's every-minute SIM-only
# check re-acquires FOREX_LOCK constantly, and a --account live run sharing
# that same lock file was left polling/losing the race against an unrelated
# process for several minutes despite LIVE and SIM touching completely
# different Saxo accounts and state files (forex_live_state.json vs
# forex_state.json) -- there was never a real cross-contamination risk to
# protect against between them, only needless contention.
FOREX_LIVE_LOCK = os.path.join(_DATA_DIR, "forex_live_runner.lock")

STALE_SECONDS = 20 * 60   # generous vs. observed ~3-4 min full scans
WAIT_TIMEOUT  = 15 * 60   # give up waiting and proceed rather than deadlock forever


def acquire(lock_path: str, label: str = "", logger=None) -> bool:
    """Blocks (polling) until no other process holds `lock_path`, then
    claims it. Never skips the caller's work -- only serializes concurrent
    callers, so nothing is silently dropped, just sequenced. A stale lock
    (holder crashed/never released) is cleared automatically. Returns True
    if the lock file was written (False only on an I/O error, in which
    case the caller proceeds unprotected rather than blocking on a broken
    filesystem)."""
    def _log(msg):
        if logger is not None:
            logger.warning(msg) if "still held" in msg.lower() else logger.info(msg)

    deadline = time.time() + WAIT_TIMEOUT
    while True:
        try:
            if os.path.exists(lock_path):
                age = time.time() - os.path.getmtime(lock_path)
                if age >= STALE_SECONDS:
                    _log(f"[lock] Stale lock ({age:.0f}s old) at {lock_path} — clearing")
                elif time.time() < deadline:
                    time.sleep(5)
                    continue
                else:
                    _log(f"[lock] Still held after {WAIT_TIMEOUT}s wait on {lock_path} — "
                         f"proceeding anyway (assuming a stuck/crashed holder)")
            with open(lock_path, "w") as f:
                f.write(f"{os.getpid()} {label} {datetime.now().isoformat()}")
            return True
        except Exception as exc:
            _log(f"[lock] Could not acquire {lock_path}: {exc} — proceeding without it")
            return False


def release(lock_path: str) -> None:
    try:
        os.remove(lock_path)
    except Exception:
        pass
