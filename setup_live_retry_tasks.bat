@echo off
REM Run this ONCE as Administrator (right-click -> Run as administrator).
REM Creates 3 IBKR Live retry tasks (exits/entries/blend) 23 min after main tasks.
powershell -NoProfile -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_live_retry_tasks.ps1"
pause
