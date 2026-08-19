@echo off
REM Windows Task Scheduler target for the Futures strategy runner.
REM Runs all 6 strategies: Donchian, RSI, EMA, MACD, BB Squeeze, MA Cross
REM Markets: ES, NQ, GC, CL, ZB
REM
REM Logs go to: logs\futures_YYYY-MM-DD.log  (written by runner.py)
REM Trade CSV : data\trades_futures.csv       (append-only, via trade_logger)
REM
REM Must run from the project root so saxo_token.json is found.

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 futures\runner.py --live >> "E:\SaxoTrNew\SaxoTrNew\data\futures_scheduler.log" 2>&1
