@echo off
REM Auto-elevates and reorders the 6 IBKR Live task triggers.
if not "%1"=="am_admin" (
    powershell -NoProfile -Command "Start-Process '%~f0' 'am_admin' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_live_task_order.ps1"
pause
