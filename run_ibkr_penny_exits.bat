@echo off
REM IBKR Penny Exits -- daily exit-condition check for open penny positions (SIM-ONLY).
REM Scheduled daily at 23:00 PKT (14:00 ET, during US market hours).
REM Exits: +25%% profit target | -12%% hard stop | 15-day time stop.
REM Runs with --execute --auto: places sell orders automatically.
REM
REM Watchdog key: "IBKR Penny Exits" -> max_log_age_hours=26

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy penny --exits --execute --auto
