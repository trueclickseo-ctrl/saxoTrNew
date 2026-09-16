@echo off
REM IBKR Gateway Watchdog -- checks port 4001 and auto-restarts IB Gateway
REM if it is down or unresponsive.
REM Runs every 5 minutes via "ATOS IBKR Gateway Watchdog" scheduled task.
REM Logs to data/ibkr_gateway_watchdog.log

cd /d E:\SaxoTrNew\SaxoTrNew
python ibkr_gateway_watchdog.py
