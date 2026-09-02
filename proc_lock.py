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
# 2026-09-03: the AI-decision SIM twin (forex/runner.py --account ai_sim) gets
# its OWN lock -- it's a separate scheduled process touching forex_state_ai.json
# / the forex_ai ledger, never the main SIM book's files, so there's no shared
# state to serialise against forex_runner.lock (same reasoning as FOREX_LIVE_LOCK).
FOREX_AI_LOCK   = os.path.join(_DATA_DIR, "forex_ai_runner.lock")

# 2026-09-02: the SIM stocks engine (atos_runner.run_cycle / run_intraday_cycle)
# had NO process lock. ATOS_LOCK serializes atos_runner-vs-atos_runner -- the
# same read-then-write race proc_lock exists for, for the case where the hourly
# `ATOS Daily Run` overlaps a watchdog-restarted copy of itself. (Housekeeping /
# safeguard concurrency vs the DB is left to SQLite WAL for now -- a per-module
# lock design for those is a separate cleanup.) ATOS_LIVE_STOCKS_LOCK is the
# real-money stocks engine's OWN lock (atos_live_stocks.py), same
# LIVE-gets-its-own-lock reasoning as FOREX_LIVE_LOCK above -- it touches
# data/atos_live_stocks.db, a completely different file from SIM's atos_live.db.
ATOS_LOCK             = os.path.join(_DATA_DIR, "atos_runner.lock")
ATOS_LIVE_STOCKS_LOCK = os.path.join(_DATA_DIR, "atos_live_stocks_runner.lock")
# 2026-09-03: the AI-decision stocks twin (atos_ai_stocks.py) -- SIM paper US
# Blend book on data/atos_ai.db, separate process, its own lock.
ATOS_AI_STOCKS_LOCK   = os.path.join(_DATA_DIR, "atos_ai_stocks_runner.lock")

STALE_SECONDS = 20 * 60   # generous vs. observed ~3-4 min full scans
WAIT_TIMEOUT  = 15 * 60   # give up waiting and proceed rather than deadlock forever


def _lock_holder_pid(lock_path: str) -> int | None:
    """The PID recorded in the lock file (first whitespace-delimited token),
    or None if it can't be read/parsed -- e.g. the file is mid-rewrite or
    uses an older format. Callers must treat None as "unknown, fall back to
    the age/timeout behaviour," never as "not held.\""""
    try:
        with open(lock_path) as f:
            return int(f.read().split()[0])
    except Exception:
        return None


def _pid_alive(pid: int) -> bool:
    """Best-effort: is `pid` still a live process? Deliberately conservative
    -- returns True on ANY uncertainty (access denied, unexpected error,
    non-Windows without os.kill support), so a lock is never stolen from a
    holder that might still be running. Found live 2026-08-31: a holder
    (intraday_monitor) crashed without releasing forex_runner.lock and,
    because acquire() only ever checked the lock file's AGE, every
    subsequent forex run then burned the full 15 min WAIT_TIMEOUT before
    proceeding -- compounding a transient network slowdown into four
    watchdog alerts."""
    if not pid or pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            # No such PID -> ERROR_INVALID_PARAMETER. Anything else
            # (e.g. ERROR_ACCESS_DENIED) means it exists -> assume alive.
            return k32.GetLastError() != ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


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
                holder_pid = _lock_holder_pid(lock_path)
                if age >= STALE_SECONDS:
                    _log(f"[lock] Stale lock ({age:.0f}s old) at {lock_path} — clearing")
                elif holder_pid is not None and not _pid_alive(holder_pid):
                    _log(f"[lock] Holder PID {holder_pid} is gone ({age:.0f}s old) at "
                         f"{lock_path} — clearing without waiting")
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
