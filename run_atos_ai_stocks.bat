@echo off
REM Windows Task Scheduler target -- AI SIM TWIN stocks scan.
REM atos_ai_stocks.py: a SIM PAPER US Blend book that trades the AI
REM basket-ranker's re-ranked pick instead of the deterministic top-N.
REM Own ledger data/atos_ai.db -- a live forward A/B vs the deterministic
REM SIM stocks book. Once daily, ~30 min after the deterministic ATOS Daily Run.

cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 atos_ai_stocks.py
