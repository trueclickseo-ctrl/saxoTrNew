@echo off
REM Windows Task Scheduler target for the ETF strategy.
REM This is a SEPARATE job from the ATOS Daily Run — it never calls
REM run_atos.py and shares no state with the shares strategies.
REM
REM Suggested schedule: daily, 30 minutes after US market open
REM (e.g. 20:00 PKT = 15:00 UTC = 10:00 ET)

REM IMPORTANT: must run from the parent app directory so saxo_auth can find
REM saxo_token.json (which lives in E:\SaxoTrNew\SaxoTrNew\, not the ETF subdir).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 saxo_etf_strategy\run_etf_bot.py >> "E:\SaxoTrNew\SaxoTrNew\data\etf_scheduler.log" 2>&1
