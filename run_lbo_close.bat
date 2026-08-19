@echo off
REM London Breakout — Force close (01:00 PKT / 20:00 UTC)
REM Time stop: closes any remaining LBO positions before new Asian session
cd /d "%~dp0"
wscript run_hidden.vbs "python forex/runner.py --strategy london_breakout --exits-only --live" data\lbo_close.log
