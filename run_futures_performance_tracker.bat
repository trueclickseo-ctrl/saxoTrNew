@echo off
REM Windows Task Scheduler target -- daily PF/P&L/WR performance tracker
REM for Futures. Runs once daily at 23:50 PKT (5 min after "ATOS Forex
REM Performance Tracker"'s 23:45) via "ATOS Futures Performance Tracker"
REM scheduled task. Explicit user request 2026-08-28: "also make advance
REM Excel tracker for ETF, Stocks, Futures -- keep track daily, weekly,
REM monthly like we have advance Excel tracker for Forex."
REM
REM Single-phase (unlike forex's tracker) -- reads directly from
REM data/pnl_ledger.db, which already stores each closed trade's real
REM dealt realized_pnl/commission, no live re-pricing needed. See
REM reports/module_performance_tracker.py's module docstring.
REM
REM Builds/overwrites ONE persistent workbook:
REM   data/futures_performance_tracker.xlsx
REM
REM Read-only analysis -- never touches a live signal, gate, stop, or order.
REM Must run from the project root so data/pnl_ledger.db is found.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1". A second inner redirect to
REM the same file races the outer one for the file handle (see
REM run_forex_daily.bat's identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
py -3.12 reports\module_performance_tracker.py futures Futures USD
