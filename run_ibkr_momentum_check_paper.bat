@echo off
REM IBKR Momentum Check PAPER AUTO -- Mon + Thu at 17:35 PKT
REM Pure momentum: exits weak positions and buys top replacements on paper account.
REM Logs to data/ibkr_momentum_check.log
REM Tracking logged to data/ibkr_momentum_tracking.db

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 run_ibkr_momentum_check.py --paper-auto
