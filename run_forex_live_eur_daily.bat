@echo off
REM Windows Task Scheduler target for the real-money Saxo LIVE EUR
REM sub-account (2026-08-26). As of the 2026-09-01 SEK consolidation this
REM account is EXITS-ONLY: LIVE_EUR_ALLOWED_STRATEGIES = set() in
REM forex/runner.py -- no new entries are placed here regardless of any
REM flag. The account still holds open rsi:* positions that need exit
REM management; that is what this run (and run_forex_live_eur_exits.bat)
REM provides. Places REAL close orders only when SAXO_LIVE_EUR_CONFIRMED=1
REM is set in this task's environment. housekeeping_live_eur.py attributes
REM pooled positions/orders by their own AccountKey field.
REM Must run from the project root so saxo_token_live.json is found (same
REM shared LIVE login/token as the SEK account -- see saxo_auth._cfg()).
REM
REM 2026-09-02 FIX: dropped --strategy rsi. The 2026-09-01 consolidation
REM emptied LIVE_EUR_ALLOWED_STRATEGIES, so runner.py's --account live_eur
REM validation hard-errored (exit 2) on every run. Omitting --strategy
REM lets the allowlist resolve to [] and the run proceeds as an
REM exits/reconcile pass (legacy-exit path for the held rsi:* positions).
REM
REM NOTE (for the user): with the account exits-only, this daily run and
REM the dedicated exit-check task overlap. Consider disabling this task and
REM keeping only "ATOS Forex LIVE EUR Exit Check".

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1". A second inner redirect to
REM the same file races the outer one for the file handle (see
REM run_forex_daily.bat's identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live_eur --exits-only --live
