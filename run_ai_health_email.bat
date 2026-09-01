@echo off
REM Windows Task Scheduler target for ai_shadow_health.py --email.
REM Sends a standalone "AI shadow study is healthy / green" status email
REM (or the problems, if any). Scheduled twice a day (09:00 + 21:00 PKT)
REM so a healthy AI bot is confirmed positively, not just alerted on when
REM broken (the scheduler_watchdog still handles problem-only alerts).
REM See ai_shadow_health.py's heartbeat_html() / send_heartbeat().

REM Do NOT redirect output here -- Task Scheduler invokes this .bat via
REM run_hidden.vbs with the log path as its 2nd argument, which wraps the
REM WHOLE .bat call in one outer ">> log 2>&1".
cd /d E:\SaxoTrNew\SaxoTrNew
pythonw -X utf8 ai_shadow_health.py --email
