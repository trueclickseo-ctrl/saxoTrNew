@echo off
REM IBKR Scorer -- daily scan of all 482-stock universe + email signal alert.
REM Runs --strategy scorer --execute --auto on paper account (auto=paper-only).
REM Scheduled Mon-Fri at 19:00 PKT (10:00 ET, 30 min after US open).
REM Logs to ibkr_scorer.log via run_hidden.vbs.
REM
REM Watchdog key: "ATOS IBKR Scorer" -> max_log_age_hours=26

cd /d E:\SaxoTrNew\SaxoTrNew
python run_ibkr_stocks.py --strategy scorer --execute --auto
