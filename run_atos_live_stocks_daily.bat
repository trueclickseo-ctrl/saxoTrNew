@echo off
REM Windows Task Scheduler target -- ATOS LIVE STOCKS (US Blend) daily cycle.
REM
REM ==> GO-LIVE (2026-09-03, explicit user instruction): this now passes --live.
REM Real orders are placed only when ALL of these also hold:
REM   SAXO_LIVE_STOCKS_CONFIRMED=1   (User env var)
REM   LIVE_STOCKS_DRY_RUN=0          (User env var)  -- "1" or unset => dry-run
REM   LIVE_STOCKS_TRADING_HALTED not set
REM Kill switch, any one of: set LIVE_STOCKS_DRY_RUN=1, remove
REM SAXO_LIVE_STOCKS_CONFIRMED, create a STOP_TRADING file, disable this task,
REM or set LIVE_STOCKS_TRADING_HALTED=1.
REM
REM Strategy is NOT in this .bat -- atos_live_stocks.py hard-codes US Blend
REM (LIVE_STOCKS_ALLOWED_STRATEGIES). Runs once daily ~40 min after the US close.

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 atos_live_stocks.py --live
