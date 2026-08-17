@echo off
REM Windows Task Scheduler target for London-session forex entries + exits.
REM Runs daily at 18:00 PKT via "ATOS Forex London Run" scheduled task.
REM Session: LONDON — 13 pairs (EUR, GBP, USD crosses; tightest spreads at London-NY overlap).
REM
REM Strategies: EMA(5/30)+ADX(14)  |  RSI(2) Pullback  |  Donchian(20)  |  BB(20,2) Reversion

cd /d E:\SaxoTrNew\SaxoTrNew
python -X utf8 forex\runner.py --live --session london >> "E:\SaxoTrNew\SaxoTrNew\data\forex_scheduler.log" 2>&1
