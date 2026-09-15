@echo off
REM Saxo SIM Scorer Portfolio -- daily scan + email signal alert.
REM Runs --entries --execute --auto on Saxo SIM paper account.
REM Scheduled Mon-Fri at 19:10 PKT (10:10 ET, 40 min after US open).
REM Logs to data/saxo_scorer.log via run_hidden.vbs.
REM
REM Watchdog key: "ATOS Saxo Scorer" -> max_log_age_hours=26

cd /d E:\SaxoTrNew\SaxoTrNew
python run_saxo_scorer.py --entries --execute --auto
