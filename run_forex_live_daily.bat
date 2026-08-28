@echo off
REM Windows Task Scheduler target for the real-money Saxo LIVE forex runner.
REM Runs once daily (conservative cadence, explicit user choice 2026-08-25 --
REM NOT the same 30-min cadence as SIM). Places REAL orders when
REM SAXO_LIVE_CONFIRMED=1 is set in this task's environment -- see
REM forex/runner.py's --account live hard rails. Strategies are hard-capped
REM in code (forex/runner.py's LIVE_ALLOWED_STRATEGIES, currently {bb}) and
REM pairs to the 17-pair HIGH_VOLUME_SYMBOLS subset, regardless of any flag
REM here.
REM
REM 2026-08-28 FIX: this .bat used to hard-code --strategy donchian,ema,rsi.
REM LIVE_ALLOWED_STRATEGIES had since changed away from that set (multiple
REM times, ending at {bb}) -- since none of donchian/ema/rsi remained
REM allowed, forex/runner.py's own --account live validation was hard-
REM erroring (argparse exit code 2) on EVERY SINGLE SCHEDULED RUN, before
REM ever reaching entries or exits. Deliberately omitting --strategy here
REM now (defaults to "all") lets forex/runner.py resolve it to
REM sorted(LIVE_ALLOWED_STRATEGIES) itself, so this .bat can never drift out
REM of sync with that allowlist again -- see forex/runner.py's argparse
REM block: `requested_strategies = requested_strategies or
REM sorted(LIVE_ALLOWED_STRATEGIES)` when --strategy is omitted.
REM Must run from the project root so saxo_token_live.json is found.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1". A second inner redirect to
REM the same file races the outer one for the file handle (see
REM run_forex_daily.bat's identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 forex\runner.py --account live --live
