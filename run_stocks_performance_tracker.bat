@echo off
REM Windows Task Scheduler target -- daily PF/P&L/WR performance tracker
REM for Stocks (ATOS US Blend + US Reversion, pnl_ledger.db module
REM 'stock'). Runs once daily at 00:00 PKT (5 min after "ATOS ETF
REM Performance Tracker"'s 23:55) via "ATOS Stocks Performance Tracker"
REM scheduled task. Explicit user request 2026-08-28: "also make advance
REM Excel tracker for ETF, Stocks, Futures -- keep track daily, weekly,
REM monthly like we have advance Excel tracker for Forex."
REM
REM Single-phase -- reads directly from data/pnl_ledger.db, no live
REM re-pricing needed. See reports/module_performance_tracker.py's
REM module docstring.
REM
REM Builds/overwrites ONE persistent workbook:
REM   data/stock_performance_tracker.xlsx  (module name is 'stock', not
REM   'stocks', in pnl_ledger.db -- kept the real module name in the
REM   filename so it's obvious which DB module this reads).
REM
REM Read-only analysis -- never touches a live signal, gate, stop, or order.
REM Must run from the project root so data/pnl_ledger.db is found.

REM Do NOT redirect output here -- see run_forex_daily.bat's identical note.
cd /d E:\SaxoTrNew\SaxoTrNew
py -3.12 reports\module_performance_tracker.py stock Stocks SEK
