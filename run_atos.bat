@echo off
title ATOS - Daily Run
cd /d "%~dp0"
echo ============================================================
echo  ATOS - Running daily trading cycle (SIM paper account)
echo ============================================================
py -3 -X utf8 run_atos.py
echo.
echo ============================================================
echo  Cycle finished. Dashboard: http://localhost:8070
echo ============================================================
pause
