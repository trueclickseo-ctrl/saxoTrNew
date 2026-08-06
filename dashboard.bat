@echo off
title ATOS - Dashboard
cd /d "%~dp0"
echo Opening ATOS dashboard at http://localhost:8070 ...
start "" http://localhost:8070
py -3 -X utf8 atos_dashboard.py
pause
