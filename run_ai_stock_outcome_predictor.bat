@echo off
REM Windows Task Scheduler target for the Stock Trade Outcome Predictor.
REM Runs DAILY at 22:10 PKT (10 min after the forex predictor, same session).
REM READ-ONLY except for writing data/stock_outcome_model/.
REM Never touches an order, position, or stop.
REM
REM Gate is built-in: if fewer than 50 closed stock trades the script
REM prints "not enough data" and exits 0 -- safe to run daily from day 1.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 ai_stock_outcome_predictor.py --train
