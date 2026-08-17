@echo off
REM Windows Task Scheduler target for the Forex strategy runner.
REM Strategy: EMA(5/30) + ADX(14) on 7 FxSpot pairs
REM Pairs: EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, NZDUSD, USDCHF
REM
REM Must run from the project root so saxo_token.json is found.

cd /d E:\SaxoTrNew\SaxoTrNew
python -X utf8 forex\runner.py --live >> "E:\SaxoTrNew\SaxoTrNew\data\forex_scheduler.log" 2>&1
