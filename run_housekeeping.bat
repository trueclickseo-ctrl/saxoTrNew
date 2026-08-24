@echo off
REM Windows Task Scheduler target for the cross-module housekeeping agent.
REM Runs a periodic safety-net reconciliation across ALL 4 modules (forex,
REM futures, etf, stocks) against live Saxo, independent of any single
REM module's own run. Exists because a module's own post-run reconciliation
REM (wired into forex/runner.py, futures/runner.py, run_etf_bot.py and
REM atos_runner.py directly) only fires when THAT module runs live -- this
REM catches drift caused by a DIFFERENT module's trade (Saxo nets opposite-
REM direction trades across strategies sharing the same instrument) even on
REM a day this module itself stays flat.
REM
REM Suggested schedule: every 30-60 minutes, any day, any time (read-only
REM unless it finds something to fix; sends email only on a mismatch).
REM See docs/housekeeping.md and housekeeping.py's module docstring.

REM Do NOT redirect output here -- Task Scheduler already invokes this .bat
REM via run_hidden.vbs with the log path as its 2nd argument, which wraps
REM the WHOLE .bat call in a single outer ">> log 2>&1". A second, inner
REM redirect to the same file races the outer one for the file handle.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 housekeeping.py
