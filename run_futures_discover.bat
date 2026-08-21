@echo off
REM Daily task (2026-08-21: was monthly, widened to daily so a contract that
REM expires mid-month — e.g. Sep 2026 WTI expired 2026-08-19, days before the
REM old monthly refresh would have caught it — is never traded stale).
REM Refreshes CL/ZB front-month UICs from Saxo, 15 min before the 06:15 daily run.
REM
REM Do NOT redirect output here — Task Scheduler already invokes this .bat
REM via run_hidden.vbs with the log path as its 2nd argument, which wraps
REM the WHOLE .bat call in a single outer ">> log 2>&1". A second, inner
REM redirect to the same file races the outer one for the file handle
REM ("process cannot access the file" — Windows sharing violation).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 futures\runner.py --discover
