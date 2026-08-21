@echo off
REM Sunday night gap fill scanner — runs at 22:00 PKT (17:00 UTC) after FX reopens.
REM Detects weekend gaps (Friday close vs Sunday open) and fades them.
REM ~80-85% of gaps fill within 5 trading days.
REM Registered as "ATOS Forex Gap Fill" in Windows Task Scheduler.

REM Do NOT redirect output here — see run_forex_daily.bat for why (Task
REM Scheduler's run_hidden.vbs already redirects the whole .bat call).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --strategy gap --live
