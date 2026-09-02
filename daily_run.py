"""
daily_run.py
------------
Scheduled daily entry point for ATOS. Runs the trading cycle ONLY if the Saxo token
is still valid; otherwise it logs and exits cleanly (never opens a browser / hangs, so
it's safe for an unattended Task Scheduler job).

The cycle itself: checks the daily risk-off switch and, on the first trading day of the
month, rebalances the US Blend. Most days it simply holds.
"""
import sys, os, json, time, ctypes as _ct
# Hide console window immediately when launched by Task Scheduler via python.exe
_hwnd = _ct.windll.kernel32.GetConsoleWindow()
if _hwnd: _ct.windll.user32.ShowWindow(_hwnd, 0)
del _ct, _hwnd

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

try:
    d = json.load(open(os.path.join(BASE, "saxo_token.json")))
    valid = (d.get("obtained_at", 0) + d.get("expires_in", 0)) - 300 > time.time()
except Exception:
    valid = False

if not valid:
    print(f"[{time.strftime('%Y-%m-%d %H:%M')}] Saxo token expired/missing — skipping run. "
          f"Refresh with: python set_token.py")
    sys.exit(0)

import atos_runner
import proc_lock
# 2026-09-02: serialize against the every-30-min ATOS Housekeeping / Safeguard
# processes (all touch data/atos_live.db). acquire() never skips work, only
# sequences it -- so a run that arrives while housekeeping holds the lock just
# waits its turn rather than racing a read-then-write.
proc_lock.acquire(proc_lock.ATOS_LOCK, "daily_run")
try:
    atos_runner.run_cycle()
finally:
    proc_lock.release(proc_lock.ATOS_LOCK)
