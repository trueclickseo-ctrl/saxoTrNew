@echo off
REM Runs every 30 minutes. Verifies every scheduled trading task actually
REM executed (not just "Task Scheduler says success") and emails an alert
REM the moment one goes silent. See scheduler_watchdog.py and
REM docs/scheduling.md for the full registry and rationale.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 scheduler_watchdog.py >> "E:\SaxoTrNew\SaxoTrNew\data\watchdog.log" 2>&1
