@echo off
REM IBKR Stocks -- US Blend V2 fortnightly rebalance.
REM Improvements over V1: skip-month momentum + volatility targeting.
REM Runs automatically via Task Scheduler: --execute --auto (no prompts).
REM
REM Manual dry-run (show plan only):
REM   python run_ibkr_stocks.py --strategy blend_v2
REM
REM Watchdog key: "IBKR Blend V2 Rebalance" -> max_log_age_hours=336 (14 days)

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy blend_v2 --execute --auto %*
