@echo off
REM Windows Task Scheduler target for the real-money Saxo LIVE EUR
REM sub-account (2026-08-26) -- RSI Pullback ONLY, on the 83 EXOTIC pairs
REM ONLY. Places REAL orders when SAXO_LIVE_EUR_CONFIRMED=1 is set in this
REM task's environment -- see forex/runner.py's --account live_eur hard
REM rails. Strategy is hard-capped to rsi and pairs to EXOTIC_SYMBOLS
REM regardless of any flag here -- enforced in code, not just this .bat's
REM args. Genuinely separate account/state/confirmation-flag from the SEK
REM LIVE account (run_forex_live_daily.bat) -- never touches its files.
REM Must run from the project root so saxo_token_live.json is found (same
REM shared LIVE login/token as the SEK account -- see saxo_auth._cfg()).

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1". A second inner redirect to
REM the same file races the outer one for the file handle (see
REM run_forex_daily.bat's identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live_eur --strategy rsi --live
