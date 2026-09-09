@echo off
REM IBKR Stocks -- US Blend fortnightly rebalance.
REM Runs automatically via Task Scheduler: --execute --auto (no prompts).
REM
REM Manual dry-run (show plan only):
REM   python run_ibkr_stocks.py --strategy blend
REM
REM Watchdog key: "IBKR Blend Rebalance" -> max_log_age_hours=336 (14 days)

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy blend --execute --auto %*
