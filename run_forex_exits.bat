@echo off
REM Intraday stop-check — runs at 14:00 PKT (London close / NY open overlap).
REM Checks stops and time-stops on all open positions. No new entries placed.
REM Registered as "ATOS Forex Exit Check" in Windows Task Scheduler.

cd /d E:\SaxoTrNew\SaxoTrNew
python -X utf8 forex\runner.py --exits-only --live >> "E:\SaxoTrNew\SaxoTrNew\data\forex_scheduler.log" 2>&1
