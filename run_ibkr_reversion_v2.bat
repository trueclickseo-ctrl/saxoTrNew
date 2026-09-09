@echo off
REM IBKR Stocks -- US Reversion V2 (SIM A/B twin with 4 improvements).
REM Improvements: SPY regime filter, EMA200 buffer, min R:R gate, volume-weighted score.
REM   run_ibkr_reversion_v2.bat           -- dry-run entries scan
REM   run_ibkr_reversion_v2.bat --execute -- confirm each new entry
REM   run_ibkr_reversion_v2.bat --exits   -- check exit conditions for open positions

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy reversion_v2 %*
