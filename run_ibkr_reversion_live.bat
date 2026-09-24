@echo off
REM IBKR Live Reversion -- US mean-reversion entries on live account U28013794.
REM Runs automatically via Task Scheduler (no interactive prompts).
REM
REM Safety gates (all must pass or the script aborts with no orders placed):
REM   1. IBKR_LIVE_CONFIRMED=1  -- must be set in User environment
REM   2. strategies.live_reversion.budget_usd > 0 in ibkr_module\config\ibkr_config.json
REM   3. IB Gateway running on port 4001 (live account U28013794)
REM   4. is_market_open() -- 09:30-16:00 ET; orders blocked outside this window
REM
REM Manual dry-run (show plan, place nothing):
REM   python run_ibkr_stocks.py --strategy reversion --live

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy reversion --live --execute --auto
