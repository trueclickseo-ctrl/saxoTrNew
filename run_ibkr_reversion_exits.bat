@echo off
REM IBKR Reversion Exits -- daily exit-condition check (dry-run).
REM Scheduled daily at 09:00 PKT (00:00 ET, after US close at 01:00 PKT).
REM Checks RSI recovery / SMA mean-reversion / time-stop for open reversion positions.
REM Review the log, then run manually with --exits --execute to actually sell.
REM
REM Watchdog key: "IBKR Reversion Exits" -> max_log_age_hours=26

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy reversion --exits
