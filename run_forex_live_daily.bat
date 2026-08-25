@echo off
REM Windows Task Scheduler target for the real-money Saxo LIVE forex runner.
REM Runs once daily (conservative cadence, explicit user choice 2026-08-25 --
REM NOT the same 30-min cadence as SIM). Places REAL orders when
REM SAXO_LIVE_CONFIRMED=1 is set in this task's environment -- see
REM forex/runner.py's --account live hard rails. Strategies are hard-capped
REM to donchian/ema/rsi and pairs to the 34 CORE_SYMBOLS regardless of any
REM flag here -- this is enforced in code, not just by this .bat's args.
REM Must run from the project root so saxo_token_live.json is found.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1". A second inner redirect to
REM the same file races the outer one for the file handle (see
REM run_forex_daily.bat's identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live --strategy donchian,ema,rsi --live
