@echo off
REM Windows Task Scheduler target for saxo_live_token_keepalive.py.
REM Runs every ~15 min, ALL DAY (not just 06:00-22:00) -- keeps the LIVE
REM refresh-token chain alive between the real trading runs, which are
REM spaced ~2h apart while the refresh_token itself only lives 1h (see
REM saxo_live_token_keepalive.py's module docstring for the full story).
REM Must run from the project root so saxo_token_live.json is found.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1". A second inner redirect to
REM the same file races the outer one for the file handle (see
REM run_forex_daily.bat's identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 saxo_live_token_keepalive.py
