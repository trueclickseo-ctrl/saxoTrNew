@echo off
REM Windows Task Scheduler target for ai/agent/strategy_evolver.py --email.
REM Runs weekly (Saturday 09:00 PKT): evolves US Blend + US Reversion AI twin
REM params, writes to atos/ai_variants/, and sends an email report.
REM See ai/agent/strategy_evolver.py and docs/atos_ai_tracker.md.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1".
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 -m ai.agent.strategy_evolver --email
