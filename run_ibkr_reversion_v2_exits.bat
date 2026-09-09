@echo off
REM IBKR -- US Reversion V2 exit check (dry-run by default).
cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy reversion_v2 --exits %*
