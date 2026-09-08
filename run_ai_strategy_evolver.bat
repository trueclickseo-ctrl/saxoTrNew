@echo off
REM Windows Task Scheduler target for ai/agent/strategy_evolver.py --email.
REM Runs DAILY at 01:30 PKT (after all NY-close scans complete):
REM   - Stocks Phase 1: us_blend + us_reversion param tuning
REM   - Forex Phase 2: entry-filter overrides for all 25 active strategies
REM   - Forex Phase 3: exit-logic overrides for all 25 active strategies
REM Writes overrides only for strategies that meet the 30-closed-trade gate.
REM Sends email digest summarising what the AI learned / changed.
REM See ai/agent/strategy_evolver.py and docs/atos_ai_tracker.md.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1".
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 -m ai.agent.strategy_evolver --email
