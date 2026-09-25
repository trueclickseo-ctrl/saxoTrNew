@echo off
REM IBKR LIVE Weekly Portfolio Report -- sends HTML email to atoslive500@gmail.com
REM Scheduled: every Saturday 21:00 PKT via Task Scheduler (captures Friday data).
python ibkr_live_weekly_report.py
