@echo off
REM Windows Task Scheduler target for the safeguard agent.
REM Runs a periodic safety-net FIX pass across ALL 4 modules (forex,
REM futures, etf, stocks) against live Saxo, independent of any single
REM module's own run. Unlike ATOS Housekeeping (report-only), this ACTS:
REM places missing/topped-up protective stops on naked positions and
REM removes local state entries proven wrong (zero live backing in their
REM own claimed direction) -- then re-verifies against a fresh Saxo
REM snapshot before declaring anything fixed, and emails the outcome.
REM
REM Exists because a module's own post-run safeguard call (wired into
REM forex/runner.py, futures/runner.py, run_etf_bot.py and atos_runner.py
REM directly) only fires when THAT module runs live -- this catches drift
REM caused by a DIFFERENT module's trade even on a day this module stays
REM flat.
REM
REM Suggested schedule: every 30-60 minutes, offset from ATOS Housekeeping.
REM See docs/housekeeping.md and safeguard.py's module docstring.

REM Do NOT redirect output here -- Task Scheduler already invokes this .bat
REM via run_hidden.vbs with the log path as its 2nd argument, which wraps
REM the WHOLE .bat call in a single outer ">> log 2>&1". A second, inner
REM redirect to the same file races the outer one for the file handle.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 safeguard.py
