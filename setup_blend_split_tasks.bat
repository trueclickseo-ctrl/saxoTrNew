@echo off
REM Auto-elevates and applies the blend-split task schedule.
if not "%1"=="am_admin" (
    powershell -NoProfile -Command "Start-Process '%~f0' 'am_admin' -Verb RunAs -Wait"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_blend_split_tasks.ps1"
pause
