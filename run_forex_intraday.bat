@echo off
REM Windows Task Scheduler target for intraday forex rescans.
REM Runs every 30 min, 06:00-22:00 PKT, via "ATOS Forex Intraday Scan" scheduled task.
REM Same full 10-strategy scan as run_forex_daily.bat/run_forex_london.bat —
REM this just adds more frequent windows so signals aren't only checked twice a day.

REM Do NOT redirect output here — see run_forex_daily.bat for why (Task
REM Scheduler's run_hidden.vbs already redirects the whole .bat call).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --live
