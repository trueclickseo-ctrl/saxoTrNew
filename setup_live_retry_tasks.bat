@echo off
REM Auto-elevates to admin if needed, then creates the 3 IBKR Live retry tasks.
if not "%1"=="am_admin" (
    powershell -NoProfile -Command "Start-Process '%~f0' 'am_admin' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_live_retry_tasks.ps1"
pause
