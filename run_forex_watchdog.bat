@echo off
REM Second, independent watchdog dedicated to Forex only. Runs every 30
REM minutes, offset from the main watchdog, and uses its own state file
REM (data/forex_watchdog_state.json) so its alert-dedup never depends on
REM the main watchdog's state. Deliberate redundancy: if the main
REM "ATOS Scheduler Watchdog" task ever silently fails the same way the
REM Futures/ETF tasks did on 2026-08-21/22, Forex -- the highest-volume,
REM highest-priority module -- is still independently monitored.
REM See scheduler_watchdog.py's --only-forex flag and docs/scheduling.md.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 scheduler_watchdog.py --only-forex >> "E:\SaxoTrNew\SaxoTrNew\data\forex_watchdog.log" 2>&1
