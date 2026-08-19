@echo off
:: start_intraday_monitor.bat — runs silently, no console popup
cd /d "E:\SaxoTrNew\SaxoTrNew"
pythonw intraday_monitor.py >> "E:\SaxoTrNew\SaxoTrNew\data\intraday_monitor.log" 2>&1
