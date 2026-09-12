@echo off
REM IBKR Penny Entries -- momentum breakout scan for sub-$2 stocks (SIM-ONLY).
REM Scheduled daily at 19:30 PKT (10:30 ET, during US market hours).
REM Runs with --execute --auto: places IBKR paper orders automatically.
REM Strategy: close > 20-day Donchian high + vol >= 2x + above SMA10.
REM Universe: PENNY_TICKERS (10 names). Max 5 slots. Budget $10k.
REM
REM Watchdog key: "IBKR Penny Entries" -> max_log_age_hours=26

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy penny --execute --auto
