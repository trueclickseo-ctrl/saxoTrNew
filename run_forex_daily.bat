@echo off
REM Windows Task Scheduler target for the Forex strategy runner.
REM Runs daily at 06:20 PKT via "ATOS Forex Daily Run" scheduled task.
REM
REM Strategies: EMA(5/30)+ADX(14)  |  RSI(2) Pullback  |  Donchian(20)  |  BB(20,2) Reversion
REM Universe:   27 FX pairs — 7 G7 majors + 20 liquid crosses (UICs confirmed Saxo SIM)
REM Max slots:  4 strategies × 4 slots = 16 open positions
REM
REM Must run from the project root so saxo_token.json is found.

cd /d E:\SaxoTrNew\SaxoTrNew
python -X utf8 forex\runner.py --live >> "E:\SaxoTrNew\SaxoTrNew\data\forex_scheduler.log" 2>&1
