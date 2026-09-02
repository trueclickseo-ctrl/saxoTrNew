@echo off
REM Windows Task Scheduler target -- AI SIM TWIN forex scan.
REM forex/runner.py --account ai_sim: a SIM PAPER book (no real orders) where
REM the Trading Copilot's resize/skip IS applied. Own ledger (forex_ai) /
REM state files -- a live forward A/B vs the deterministic SIM forex book.
REM Same 5-strategy roster / 184 pairs. --live here means "act on SIM paper".
REM Runs on the same cadence as "ATOS Forex Intraday Scan".

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account ai_sim --live
