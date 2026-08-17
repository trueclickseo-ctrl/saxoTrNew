@echo off
REM Sunday night gap fill scanner — runs at 22:00 PKT (17:00 UTC) after FX reopens.
REM Detects weekend gaps (Friday close vs Sunday open) and fades them.
REM ~80-85% of gaps fill within 5 trading days.
REM Registered as "ATOS Forex Gap Fill" in Windows Task Scheduler.

cd /d E:\SaxoTrNew\SaxoTrNew
python -X utf8 forex\runner.py --strategy gap --live >> "E:\SaxoTrNew\SaxoTrNew\data\forex_scheduler.log" 2>&1
