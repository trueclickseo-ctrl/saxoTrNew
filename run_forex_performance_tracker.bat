@echo off
REM Windows Task Scheduler target -- daily PF/P&L/WR performance tracker.
REM Runs once daily at 23:45 PKT (15 min after "ATOS Daily Summary", so the
REM day's last trades/closes are already reflected) via "ATOS Forex
REM Performance Tracker" scheduled task. Explicit user request 2026-08-28:
REM "Track the performance of Strategies Group Wise and Track the
REM Performance of Pairs wise... Important do this everyday when the
REM trading is close."
REM
REM Two-phase design (same reason as reports/daily_sim_report.py -- the
REM normal project Python has forex.runner/torch but not openpyxl on
REM whichever install has that; py -3.12 has openpyxl but not torch):
REM   1) python   reports/_gather_daily_sim_data.py           -- real Saxo data
REM   2) py -3.12 reports/pair_group_performance_tracker.py   -- builds/updates
REM      the ONE persistent workbook at data/forex_performance_tracker.xlsx
REM      (overwritten in place every run -- not a new dated file each day,
REM      per explicit "keep that sheet with you" request).
REM
REM Read-only analysis -- never touches a live signal, gate, stop, or order.
REM Must run from the project root so saxo_token.json is found.

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1". A second inner redirect to
REM the same file races the outer one for the file handle (see
REM run_forex_daily.bat's identical note).
cd /d E:\SaxoTrNew\SaxoTrNew
python reports\_gather_daily_sim_data.py
py -3.12 reports\pair_group_performance_tracker.py
