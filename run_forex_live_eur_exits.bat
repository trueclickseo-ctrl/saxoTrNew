@echo off
REM Windows Task Scheduler target -- LIVE EUR sub-account exit/stop check.
REM Checks stops and time-stops on open LIVE EUR positions only -- no new
REM entries placed here. Same SAXO_LIVE_EUR_CONFIRMED=1 requirement as
REM run_forex_live_eur_daily.bat.
REM
REM 2026-09-02 FIX: this hard-coded --strategy rsi, but the 2026-09-01 SEK
REM consolidation set LIVE_EUR_ALLOWED_STRATEGIES = set() (EUR is exits-only
REM now, no strategy). runner.py's --account live_eur validation then
REM hard-errored (exit 2) on every scheduled run since -- no trailing stops,
REM no time-stops, no state reconciliation on the open EUR positions for
REM ~2 days. Same bug class as run_forex_live_exits.bat's 2026-08-28 fix.
REM Omitting --strategy lets runner.py resolve it to LIVE_EUR_ALLOWED_
REM STRATEGIES itself; run_exits_only() then manages the open rsi:* EUR
REM positions via its legacy-exit path (_legacy_exit_strategies).

REM Do NOT redirect output here -- see run_forex_live_eur_daily.bat for why.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live_eur --exits-only --live
