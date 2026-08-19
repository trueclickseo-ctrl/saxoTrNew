@echo off
REM Monthly task — refreshes CL and ZB front-month UICs from Saxo.
REM Run on the 1st of each month so futures runner stays on the correct contract.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 futures\runner.py --discover >> "E:\SaxoTrNew\SaxoTrNew\data\futures_discover.log" 2>&1
