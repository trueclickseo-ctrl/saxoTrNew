@echo off
REM IBKR Stocks Trail Stops -- ratchet stop-loss orders upward.
REM Runs daily at 21:00 PKT (12:00 ET, midday US session).
REM
REM Protective only: stops only ever raised, never lowered. No new positions.
REM Requires IB Gateway running on localhost:7497 (paper) or 7496 (live).
REM
REM Scheduled via setup_scheduler_ibkr_trail_stops.ps1 as
REM "ATOS IBKR Trail Stops". Watchdog monitors: ibkr_trail_stops.log

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --trail-stops --execute
