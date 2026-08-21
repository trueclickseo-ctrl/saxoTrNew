@echo off
:: ============================================================
:: run_atos_daily.bat
:: Scheduled daily launcher for ATOS.
:: Runs every weekday at 02:00 AM PKT (= 21:00 UTC)
:: one hour after US market close (4 PM ET / 9 PM GMT).
:: ============================================================

cd /d "E:\SaxoTrNew\SaxoTrNew"

:: atos_runner.py now trades via IBKR (ibkr_client.py), not Saxo -- no token
:: to load here. Make sure IB Gateway is already running and logged into
:: the paper account before this fires; the socket connect fails loudly
:: (with a retry) if it isn't. IBKR_HOST/IBKR_PORT/IBKR_CLIENT_ID default to
:: 127.0.0.1:4002/1 -- set them here if your Gateway uses different values.

:: Start atos_dashboard.py if not already running on :8070
netstat -ano | findstr ":8070 " | findstr "LISTENING" >nul 2>&1
if errorlevel 1 (
    echo Starting ATOS Dashboard on http://localhost:8070 ...
    start "ATOS Dashboard" /MIN powershell -NoLogo -NoProfile -Command ^
        "Set-Location 'E:\SaxoTrNew\SaxoTrNew'; python atos_dashboard.py"
    timeout /t 3 /nobreak >nul
) else (
    echo ATOS Dashboard already running on :8070
)

:: Open the live dashboard in the default browser
start http://localhost:8070

:: Run the ATOS daily cycle
python atos_runner.py

:: Keep window open so you can read the output
echo.
echo Scan complete — press any key to close this window.
pause >nul
