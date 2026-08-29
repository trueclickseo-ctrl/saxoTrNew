@echo off
REM Windows Task Scheduler target for saxo_sim_token_keepalive.py.
REM Runs every ~15 min, ALL DAY -- keeps the SIM refresh-token chain alive
REM across the overnight gap (the ATOS Forex Intraday Scan only runs
REM 06:05 -> ~03:00 PKT, then ~3h with nothing, while a PKCE SIM
REM refresh_token only lives ~60 min -- see saxo_sim_token_keepalive.py's
REM module docstring for the full story). Must run from the project root
REM so saxo_token.json is found.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1" (see run_forex_daily.bat's
REM identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 saxo_sim_token_keepalive.py
