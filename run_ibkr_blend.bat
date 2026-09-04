@echo off
REM IBKR Stocks -- US Blend fortnightly rebalance (dry-run by default).
REM Strategy: cross-sectional momentum, 8 positions, Yahoo Finance signal.
REM Run manually when the fortnightly window arrives:
REM   run_ibkr_blend.bat           -- show plan, place nothing
REM   run_ibkr_blend.bat --execute -- confirm each trade

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy blend %*
