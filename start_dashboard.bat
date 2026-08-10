@echo off
:: ============================================================
:: start_dashboard.bat
:: Starts the ATOS live dashboard on http://localhost:8070
:: Scheduled to run at US market open (6:30 PM PKT weekdays)
:: Can also be run manually anytime.
:: ============================================================

cd /d "E:\SaxoTrNew\SaxoTrNew"

:: Check if already running
netstat -ano | findstr ":8070 " | findstr "LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo ATOS Dashboard already running on http://localhost:8070
    start http://localhost:8070
    timeout /t 3
    exit /b 0
)

echo Starting ATOS Dashboard...
start "ATOS Dashboard" powershell -NoLogo -NoProfile -Command ^
    "Set-Location 'E:\SaxoTrNew\SaxoTrNew'; Write-Host ''; Write-Host '  ATOS Dashboard — http://localhost:8070' -ForegroundColor Green; Write-Host '  Press Ctrl+C to stop.' -ForegroundColor Gray; Write-Host ''; python atos_dashboard.py"

:: Wait for server to start, then open browser
timeout /t 3 /nobreak >nul
start http://localhost:8070
echo Dashboard opened at http://localhost:8070
