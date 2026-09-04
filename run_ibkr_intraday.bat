@echo off
REM IBKR Stocks -- Intraday Reversion scan (5-min bars, US session only).
REM US hours: 09:30-16:00 ET = 18:30-01:00 PKT.
REM   run_ibkr_intraday.bat           -- dry-run scan
REM   run_ibkr_intraday.bat --execute -- confirm each entry

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy intraday %*
