@echo off
REM Windows Task Scheduler target for London-session forex entries + exits.
REM Runs daily at 18:00 PKT via "ATOS Forex London Run" scheduled task.
REM Session: ALL — full 34-pair universe (was LONDON-only/20 pairs; widened
REM 2026-08-20 so every strategy always scans the max tradeable universe).
REM
REM Strategies: EMA(5/30)+ADX(14)  |  RSI(2) Pullback  |  Donchian(20)  |  BB(20,2) Reversion

REM Do NOT redirect output here — see run_forex_daily.bat for why (Task
REM Scheduler's run_hidden.vbs already redirects the whole .bat call).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --live
