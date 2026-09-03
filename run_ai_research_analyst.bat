@echo off
REM Windows Task Scheduler target for the AI Research Analyst (roadmap #19).
REM OFFLINE + READ-ONLY: replays each SIM-roster strategy over ~13y of Yahoo
REM daily bars (decomposition harness), aggregates the closed-trade record +
REM the AI Trading Journal into a digest, has an LLM propose SPECIFIED,
REM testable strategy filters, auto-runs the cheap decomposition gate, and
REM appends to the triaged backlog (data/ai_research_hypotheses.jsonl). It
REM NEVER edits a strategy or touches an order. --sweep refreshes the
REM decomposition cache first (the slow part -- a weekly job). Gated by
REM config/ai.json research_analyst.enabled (ships OFF).

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1".
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 ai_research_analyst.py --sweep
