@echo off
REM IBKR Stocks -- US Reversion entries (daily scan, PKT morning before US open).
REM Scan for oversold dips: RSI < 38, price 5% below SMA20, vol spike.
REM   run_ibkr_reversion.bat           -- dry-run entries scan
REM   run_ibkr_reversion.bat --execute -- confirm each new entry
REM   run_ibkr_reversion.bat --exits   -- check exit conditions for open positions

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy reversion %*
