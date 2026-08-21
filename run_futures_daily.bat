@echo off
REM Windows Task Scheduler target for the Futures strategy runner.
REM Runs all 6 strategies: Donchian, RSI, EMA, MACD, BB Squeeze, MA Cross
REM Markets: ES, NQ, GC, CL, ZB
REM
REM Logs go to: logs\futures_YYYY-MM-DD.log  (written by runner.py)
REM Trade CSV : data\trades_futures.csv       (append-only, via trade_logger)
REM
REM Must run from the project root so saxo_token.json is found.
REM
REM Do NOT redirect output here — Task Scheduler already invokes this .bat
REM via run_hidden.vbs with the log path as its 2nd argument, which wraps
REM the WHOLE .bat call in a single outer ">> log 2>&1". A second, inner
REM redirect to the same file races the outer one for the file handle
REM ("process cannot access the file" — Windows sharing violation), and can
REM crash or hang the run before it ever reaches the strategy scan (same bug
REM class already fixed in the forex .bat files on 2026-08-20; missed here).

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 futures\runner.py --live
