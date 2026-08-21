@echo off
REM Intraday stop-check — runs at 14:00 PKT (London close / NY open overlap).
REM Checks stops and time-stops on all open positions. No new entries placed.
REM Registered as "ATOS Forex Exit Check" in Windows Task Scheduler.

REM Do NOT redirect output here — see run_forex_daily.bat for why (Task
REM Scheduler's run_hidden.vbs already redirects the whole .bat call).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --exits-only --live
