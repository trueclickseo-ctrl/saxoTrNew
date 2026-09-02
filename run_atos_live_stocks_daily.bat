@echo off
REM Windows Task Scheduler target -- ATOS LIVE STOCKS (US Blend) daily cycle.
REM
REM PHASE 1 = OBSERVE ONLY. This wrapper deliberately does NOT pass --live:
REM atos_live_stocks.py stays in dry-run (would-be orders + AI cards logged,
REM ZERO real orders) until Phase 2, which is a separate explicit go-live
REM step (add --live here AND set SAXO_LIVE_STOCKS_CONFIRMED=1 +
REM LIVE_STOCKS_DRY_RUN=0 as User env vars, then reboot).
REM
REM Strategy is NOT in this .bat -- atos_live_stocks.py hard-codes US Blend
REM (LIVE_STOCKS_ALLOWED_STRATEGIES) so it can never drift here.
REM Runs once daily ~40 min after the US close.

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 atos_live_stocks.py
