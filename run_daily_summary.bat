@echo off
REM Windows Task Scheduler target for the end-of-day summary email.
REM Runs once daily, late enough to capture the full trading day: per-
REM strategy trade count, symbols traded, win rate, profit factor across
REM all 4 modules (forex/futures/etf/stocks), plus account equity/margin
REM and a naked-position health check.
REM
REM Suggested schedule: daily, 23:30 PKT (captures the NY session close
REM and any late exits/time-stops for the day).
REM See docs/housekeeping.md and daily_summary.py's module docstring.

REM Do NOT redirect output here -- Task Scheduler already invokes this .bat
REM via run_hidden.vbs with the log path as its 2nd argument, which wraps
REM the WHOLE .bat call in a single outer ">> log 2>&1". A second, inner
REM redirect to the same file races the outer one for the file handle.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 daily_summary.py
