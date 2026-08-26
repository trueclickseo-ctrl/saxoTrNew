@echo off
REM London Breakout — NY Open (18:00 PKT / 13:00 UTC)
REM Trades break of London morning range (09:00-12:00 UTC, LONDON_RANGE_END in
REM forex/strategy_london_breakout.py) — checked 1h after that range closes to
REM see if price actually broke out, not at the instant it closes.
REM
REM Do NOT redirect output here or wrap this in a second run_hidden.vbs call.
REM The old double-wrap (Task Scheduler -> vbs -> this .bat -> vbs #2 -> python)
REM was broken: passing a "program + arguments" string through run_hidden.vbs's
REM quote-stripping a second time only works for a bare file path with no
REM embedded spaces/args — with real args it produces "system cannot find the
REM path specified" and nothing ever gets logged. Task Scheduler now invokes
REM this .bat directly via a single run_hidden.vbs wrap with the log path as
REM its own 2nd argument, exactly like the working forex daily/exits tasks.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --strategy london_breakout --live
