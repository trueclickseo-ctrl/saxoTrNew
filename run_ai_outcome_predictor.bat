@echo off
REM Windows Task Scheduler target for the Trade Outcome Predictor (roadmap #20).
REM Runs DAILY at 22:00 PKT -- after the US session closes, before the 23:30
REM daily-summary email. READ-ONLY except for writing data/trade_outcome_model/.
REM Never touches an order, position, or stop.
REM
REM Gate is built-in: if fewer than 100 closed cards from active strategies
REM (rsi/rsi_trend/rsi_atr/ema_trend/bb_quality/zscore_quality) the script
REM prints "not enough data" and exits 0 -- safe to run daily from day 1.
REM When the gate clears it trains the model and saves model.pkl + report.json.
REM The model only influences live proposals once config/ai.json
REM outcome_predictor.enabled is set to true (human step after reviewing --report).
REM
REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1".
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 ai_outcome_predictor.py --train
