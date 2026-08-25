@echo off
REM Windows Task Scheduler target -- LIVE account exit/stop check.
REM Runs once daily. Checks stops and time-stops on open LIVE positions
REM only -- no new entries placed here. Same SAXO_LIVE_CONFIRMED=1
REM requirement and strategy/pair hard rails as run_forex_live_daily.bat.

REM Do NOT redirect output here -- see run_forex_live_daily.bat for why.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live --strategy donchian,ema,rsi --exits-only --live
