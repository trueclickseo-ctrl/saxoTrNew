@echo off
REM IBKR Bagger Entries -- high-momentum 80%+ 6m ROC continuation scan (SIM-ONLY).
REM Scheduled daily at 19:30 PKT (10:30 ET, during US market hours).
REM Runs with --execute --auto: places IBKR paper orders automatically.
REM Strategy: 80%+ 6m ROC + within 15%% of 52w high + vol trend up + RSI 40-75 + above SMA50.
REM Universe: BAGGER_TICKERS (65 names). Max 5 slots. Budget $7k. 12%% trailing stop.
REM
REM Watchdog key: "IBKR Bagger Entries" -> max_log_age_hours=26

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy bagger --execute --auto
