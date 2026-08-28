@echo off
REM Windows Task Scheduler target -- LIVE EUR sub-account exit/stop check.
REM Checks stops and time-stops on open LIVE EUR positions only -- no new
REM entries placed here. Same SAXO_LIVE_EUR_CONFIRMED=1 requirement and
REM strategy/pair hard rails as run_forex_live_eur_daily.bat (2026-08-28:
REM same 17-pair HIGH_VOLUME_SYMBOLS universe as the SEK account, plus any
REM legacy exotic-pair positions).

REM Do NOT redirect output here -- see run_forex_live_eur_daily.bat for why.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live_eur --strategy rsi --exits-only --live
