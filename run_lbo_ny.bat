@echo off
REM London Breakout — NY Open (18:00 PKT / 13:00 UTC)
REM Trades break of London morning range (09:00-13:00 UTC)
cd /d "%~dp0"
wscript run_hidden.vbs "python forex/runner.py --strategy london_breakout --live" data\lbo_ny.log
