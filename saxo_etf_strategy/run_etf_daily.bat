@echo off
REM Windows Task Scheduler target for the ETF strategy.
REM This is a SEPARATE job from the ATOS Daily Run — it never calls
REM run_atos.py and shares no state with the shares strategies.
REM
REM Suggested schedule: daily, 30 minutes after US market open
REM (e.g. 20:00 PKT = 15:00 UTC = 10:00 ET)

REM IMPORTANT: must run from the parent app directory so saxo_auth can find
REM saxo_token.json (which lives in E:\SaxoTrNew\SaxoTrNew\, not the ETF subdir).
REM
REM Do NOT redirect output here — Task Scheduler already invokes this .bat
REM via run_hidden.vbs with the log path as its 2nd argument, which wraps
REM the WHOLE .bat call in a single outer ">> log 2>&1". A second, inner
REM redirect to the same file races the outer one for the file handle
REM ("process cannot access the file" — Windows sharing violation), and can
REM crash the run before the rotation scan ever completes (same bug class
REM found and fixed in forex/futures .bat files; missed here — likely why
REM ETF hasn't re-scanned/rotated since the original 2026-08-17 entries).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 saxo_etf_strategy\run_etf_bot.py
