@echo off
REM IBKR Reversion Entries -- daily scan for oversold dip setups (dry-run).
REM Scheduled daily at 16:00 PKT (07:00 ET, before US market open at 18:30 PKT).
REM Logs signals to ibkr_reversion_entries.log via run_hidden.vbs.
REM Review the log, then run manually with --execute to actually buy.
REM
REM Watchdog key: "IBKR Reversion Entries" -> max_log_age_hours=26

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy reversion
