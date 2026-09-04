@echo off
REM IBKR Stocks -- 4 US Signals strategies (SMA Crossover, RSI Reversal,
REM US Momentum, US Ensemble).
REM
REM Default (no args): dry-run entries scan -- shows signals, places nothing.
REM   run_ibkr_signals.bat           -- entries dry-run
REM   run_ibkr_signals.bat --exits   -- exits dry-run
REM   run_ibkr_signals.bat --execute -- confirm each entry interactively
REM
REM Scheduled via setup_scheduler_ibkr_signals.ps1:
REM   "ATOS IBKR Signals Entries" -- daily 16:00 PKT (07:00 ET, pre-open)
REM   "ATOS IBKR Signals Exits"   -- daily 09:00 PKT (00:00 ET, post-close)

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy signals --execute --auto %*
