@echo off
REM Avanza ISK sleeve — PowerShell dashboard.
REM Prints account, positions, open orders, last signal, and recent trades.
REM Read-only: never places orders.
REM
REM Usage:
REM   run_avanza_dashboard.bat          print once and exit
REM   run_avanza_dashboard.bat -Watch   refresh every 60 seconds

cd /d E:\SaxoTrNew\SaxoTrNew
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File dashboard_avanza.ps1 %*
