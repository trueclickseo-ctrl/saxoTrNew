@echo off
REM Windows Task Scheduler target for the Futures strategy runner.
REM Runs all 3 strategies: Donchian + RSI(2) Pullback + EMA Crossover
REM Markets: ES, NQ, GC, CL, ZB
REM
REM Must run from the project root so saxo_token.json is found.

cd /d E:\SaxoTrNew\SaxoTrNew
python -X utf8 futures\runner.py --live >> "E:\SaxoTrNew\SaxoTrNew\data\futures_scheduler.log" 2>&1
