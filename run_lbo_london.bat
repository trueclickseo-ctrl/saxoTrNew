@echo off
REM London Breakout — London Open (12:00 PKT / 07:00 UTC)
REM Trades break of Asian session range (00:00-07:00 UTC)
REM
REM Do NOT redirect output here or wrap this in a second run_hidden.vbs call.
REM The old double-wrap (Task Scheduler -> vbs -> this .bat -> vbs #2 -> python)
REM was broken: passing a "program + arguments" string through run_hidden.vbs's
REM quote-stripping a second time only works for a bare file path with no
REM embedded spaces/args — with real args it produces "system cannot find the
REM path specified" and nothing ever gets logged (confirmed 2026-08-21: this
REM is why lbo_london.log was never actually updated by the real scheduled
REM task despite LastTaskResult showing success). Task Scheduler now invokes
REM this .bat directly via a single run_hidden.vbs wrap with the log path as
REM its own 2nd argument, exactly like the working forex daily/exits tasks.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --strategy london_breakout --live
