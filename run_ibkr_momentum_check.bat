@echo off
REM IBKR Momentum Check -- twice-weekly (Mon + Thu) at 17:30 PKT
REM Scores held blend positions; flags weak ones (score < 55 or rank > 120)
REM for manual review. Semi-manual: report only, no automatic exits.
REM Logs to data/ibkr_momentum_check.log

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 run_ibkr_momentum_check.py --live
