@echo off
REM Windows Task Scheduler target -- LIVE account exit/stop check.
REM Runs once daily. Checks stops and time-stops on open LIVE positions
REM only -- no new entries placed here. Same SAXO_LIVE_CONFIRMED=1
REM requirement and strategy/pair hard rails as run_forex_live_daily.bat.
REM
REM 2026-08-28 FIX: same bug as run_forex_live_daily.bat -- this hard-coded
REM --strategy donchian,ema,rsi, none of which remain in
REM LIVE_ALLOWED_STRATEGIES (now {bb}), so --account live's own validation
REM was hard-erroring on every scheduled run before even checking exits.
REM Omitting --strategy lets forex/runner.py resolve it to
REM LIVE_ALLOWED_STRATEGIES itself so this can't drift out of sync again.

REM Do NOT redirect output here -- see run_forex_live_daily.bat for why.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live --exits-only --live
