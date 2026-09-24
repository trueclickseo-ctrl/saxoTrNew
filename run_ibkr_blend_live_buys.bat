@echo off
REM IBKR Live Blend BUYS ONLY -- runs after reversion has consumed its share of cash.
REM Run at 21:00 PKT; sells ran at 19:30 via run_ibkr_blend_live_sells.bat
REM Schedule: 19:30 blend sells -> 20:10 reversion exits -> 20:35 reversion buys -> 21:00 blend buys

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy blend --live --execute --auto --buys-only
