@echo off
REM IBKR Live Blend SELLS ONLY -- frees cash for reversion entries before blend buys.
REM Run at 19:30 PKT; blend buys run separately at 21:00 PKT via run_ibkr_blend_live_buys.bat
REM Schedule: 19:30 blend sells -> 20:10 reversion exits -> 20:35 reversion buys -> 21:00 blend buys

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy blend --live --execute --auto --sells-only
