@echo off
REM London Breakout — London Open (12:00 PKT / 07:00 UTC)
REM Trades break of Asian session range (00:00-07:00 UTC)
cd /d "%~dp0"
wscript run_hidden.vbs "python forex/runner.py --strategy london_breakout --live" data\lbo_london.log
