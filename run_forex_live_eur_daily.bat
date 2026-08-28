@echo off
REM Windows Task Scheduler target for the real-money Saxo LIVE EUR
REM sub-account (2026-08-26) -- RSI Pullback ONLY. As of 2026-08-28 trades
REM the SAME 17-pair HIGH_VOLUME_SYMBOLS universe as the SEK LIVE account
REM (no more exotic pairs live) -- safe because housekeeping_live_eur.py
REM attributes pooled positions/orders by their own AccountKey field, not
REM pair-tier membership. Places REAL orders when SAXO_LIVE_EUR_CONFIRMED=1
REM is set in this task's environment -- see forex/runner.py's --account
REM live_eur hard rails. Strategy/pairs are hard-capped in code
REM (LIVE_EUR_ALLOWED_STRATEGIES / _filter_pairs_for_account()) regardless
REM of any flag here. Genuinely separate account/state/confirmation-flag
REM from the SEK LIVE account (run_forex_live_daily.bat) -- never touches
REM its files.
REM Must run from the project root so saxo_token_live.json is found (same
REM shared LIVE login/token as the SEK account -- see saxo_auth._cfg()).
REM
REM 2026-08-28: kept --strategy rsi explicit here (unlike the SEK .bat,
REM which now omits --strategy) since LIVE_EUR_ALLOWED_STRATEGIES has never
REM changed and rsi remains valid -- but the same "let the allowlist
REM resolve it" fix applies if this account ever gets a second strategy.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1". A second inner redirect to
REM the same file races the outer one for the file handle (see
REM run_forex_daily.bat's identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live_eur --strategy rsi --live
