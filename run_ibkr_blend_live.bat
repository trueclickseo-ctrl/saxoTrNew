@echo off
REM IBKR Live Blend -- US Blend fortnightly rebalance on live ISK account U28013794.
REM Runs automatically via Task Scheduler (no interactive prompts).
REM
REM Safety gates (all must pass or the script aborts with no orders placed):
REM   1. IBKR_LIVE_CONFIRMED=1  -- must be set in User environment
REM   2. strategies.live_blend.budget_usd > 0 in ibkr_module\config\ibkr_config.json
REM   3. IB Gateway running on port 4002 (live account U28013794)
REM   4. is_market_open() -- 09:30-16:00 ET; orders blocked outside this window
REM
REM Both BUYs and SELLs are handled in one pass (positions not in target basket
REM are sold; new targets are bought; stop-loss placed immediately after each fill).
REM
REM Manual dry-run (show plan, place nothing):
REM   python run_ibkr_stocks.py --strategy blend --live

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy blend --live --execute --auto
