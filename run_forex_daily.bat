@echo off
REM Windows Task Scheduler target for the Forex strategy runner.
REM Runs daily at 06:20 PKT via "ATOS Forex Daily Run" scheduled task.
REM Session: ALL — full 34-pair universe (was ASIAN-only/14 pairs; widened
REM 2026-08-20 so every strategy always scans the max tradeable universe).
REM
REM Strategies: EMA(5/30)+ADX  |  RSI(2)  |  Donchian(20)  |  BB(20,2)  |  Pullback-to-EMA(20)
REM Must run from the project root so saxo_token.json is found.

REM Do NOT redirect output here — Task Scheduler already invokes this .bat
REM via run_hidden.vbs with the log path as its 2nd argument, which wraps
REM the WHOLE .bat call in a single outer ">> log 2>&1". Adding a second,
REM inner redirect to the same file races the outer one for the file handle
REM ("process cannot access the file" — Windows sharing violation), and can
REM silently drop that run's output.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --live
