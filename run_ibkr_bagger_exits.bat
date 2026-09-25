@echo off
REM IBKR Bagger Exits -- daily exit-condition check for open bagger positions (SIM-ONLY).
REM Scheduled daily at 23:00 PKT (14:00 ET, during US market hours).
REM Exits: 12%% trailing stop | 60-day time stop.
REM Runs with --execute --auto: places sell orders automatically.
REM
REM Watchdog key: "IBKR Bagger Exits" -> max_log_age_hours=26

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy bagger --exits --execute --auto
