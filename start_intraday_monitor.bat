@echo off
:: ============================================================
:: start_intraday_monitor.bat
:: Starts the ATOS intraday stop-loss monitor.
:: Scheduled at 6:25 PM PKT weekdays (5 min before US open).
:: ============================================================

cd /d "E:\SaxoTrNew\SaxoTrNew"

:: Load Saxo token
for /f "tokens=*" %%T in ('python -c "import json; print(json.load(open('saxo_token.json'))['access_token'])"') do (
    set SAXO_TOKEN=%%T
)

echo.
echo  ATOS Intraday Stop-Loss Monitor
echo  Checking every second during US market hours.
echo  Log: data\intraday_monitor.log
echo  Press Ctrl+C to stop.
echo.

python intraday_monitor.py
