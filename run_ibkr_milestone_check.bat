@echo off
REM Called after each IBKR execution cycle by the scheduler.
REM Counts valid closed trades per strategy.
REM When any strategy crosses a new multiple of 20 closed trades:
REM   - calls Claude (Haiku) for a parameter-tuning suggestion
REM   - saves proposal to data/ibkr_strategy_proposals.json (human review only)
REM   - sends email notification
REM See ibkr_milestone_check.py for details.

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 ibkr_milestone_check.py
