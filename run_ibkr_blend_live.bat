@echo off
REM IBKR Live Blend -- US Blend rebalance on live ISK account U28013794.
REM
REM SAFETY GATES (must all pass or the script aborts):
REM   1. IBKR_LIVE_CONFIRMED=1 must be set in User environment
REM        setx IBKR_LIVE_CONFIRMED 1   (restart terminal after)
REM   2. strategies.live_blend.budget_usd > 0 in ibkr_module\config\ibkr_config.json
REM   3. IB Gateway must be running on port 4002 (live)
REM
REM Usage:
REM   run_ibkr_blend_live.bat            -- dry-run: shows plan, places nothing
REM   run_ibkr_blend_live.bat --execute  -- interactive: confirm each trade

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy blend --live %*
