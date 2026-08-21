@echo off
REM Windows Task Scheduler target for daily per-strategy performance charts.
REM Runs at 23:15 PKT, 15 min after "ATOS PnL Sync" (23:00 PKT) so charts
REM reflect the fully-synced day across all 4 modules (stock/etf/futures/forex).
REM Generates data/charts/{module}_strategy_YYYY-MM-DD.png (dated, permanent)
REM and data/charts/{module}_strategy_latest.png (always-current) for each.
REM
REM Do NOT redirect output here — Task Scheduler already invokes this .bat
REM via run_hidden.vbs with the log path as its 2nd argument, which wraps
REM the WHOLE .bat call in a single outer ">> log 2>&1". A second, inner
REM redirect to the same file races the outer one for the file handle
REM (Windows sharing violation) — same bug class already fixed in every
REM other module's .bat file this session; don't reintroduce it here.
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 daily_chart.py
