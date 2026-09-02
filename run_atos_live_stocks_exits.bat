@echo off
REM Windows Task Scheduler target -- ATOS LIVE STOCKS exit-check backstop.
REM Manages open US Blend positions only (risk-off / corp-event exits), no new
REM buys. Runs once daily at 14:00 PKT.
REM
REM ==> GO-LIVE (2026-09-03): passes --live so it actually acts on the real
REM book. Same SAXO_LIVE_STOCKS_CONFIRMED=1 + LIVE_STOCKS_DRY_RUN=0 requirement
REM and kill switches as run_atos_live_stocks_daily.bat.

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 atos_live_stocks.py --live --exits-only
