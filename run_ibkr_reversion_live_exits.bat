@echo off
REM IBKR Live Reversion Exits -- check and close bounced positions on live account U28013794.
REM Runs automatically via Task Scheduler (no interactive prompts).
REM
REM Manual dry-run (show exit plan, place nothing):
REM   python run_ibkr_stocks.py --strategy reversion --exits --live

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy reversion --exits --live --execute --auto
