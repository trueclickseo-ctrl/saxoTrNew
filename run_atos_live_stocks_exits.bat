@echo off
REM Windows Task Scheduler target -- ATOS LIVE STOCKS exit-check backstop.
REM Manages open US Blend positions only (stop / risk-off / event exits),
REM no new buys. PHASE 1 = OBSERVE ONLY (no --live) -- see
REM run_atos_live_stocks_daily.bat. Runs once daily at 14:00 PKT.

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 atos_live_stocks.py --exits-only
