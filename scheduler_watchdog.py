"""
scheduler_watchdog.py
----------------------
Verifies that every scheduled trading task actually ran, and emails an
immediate alert the moment one didn't — instead of the silent-failure
pattern discovered 2026-08-20 (run_hidden.vbs reported success while doing
nothing for two straight days; see docs/scheduling.md).

Two independent checks per task:
  1. Windows Task Scheduler's own LastTaskResult (0 = ok, 267011 = "has
     never run yet", anything else = a real failure).
  2. Log-file freshness: the task's log must have a modification time at or
     after its own LastRunTime (minus a small clock-skew tolerance). This is
     the check that actually catches "reported success but did nothing" —
     LastTaskResult alone would NOT have caught the run_hidden.vbs bug.

Claude-native tasks (LBO) have no Windows Task Scheduler entry, so only the
log-freshness check applies, against a hardcoded expected-fire schedule.

Usage:
    python scheduler_watchdog.py            # run one check pass
    python scheduler_watchdog.py --verbose   # print status for every task, not just failures

Scheduled via Windows Task Scheduler as "ATOS Scheduler Watchdog", every 30
minutes, through the same run_hidden.vbs launcher (fixed 2026-08-20) all
other tasks use.

To add a new watched task: add one entry to WINDOWS_TASKS or CLAUDE_TASKS
below. No other code changes needed.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
EMAIL_CFG  = os.path.join(BASE_DIR, "config", "email.json")
STATE_FILE = os.path.join(DATA_DIR, "watchdog_state.json")

# "Has not run yet" placeholder result code — not a real failure.
TASK_NEVER_RUN = 267011

# Suppress re-alerting the same task within this window once it's been
# flagged, so a still-broken task doesn't spam an email every 30 minutes —
# but it WILL alert again after this window if still unresolved.
REALERT_AFTER_HOURS = 4

# ── Registry: Windows Task Scheduler-backed tasks ────────────────────────────
# name              -> (task_name_in_windows, log_file, grace_minutes)
# grace_minutes: how long after LastRunTime the log is allowed to lag before
# we call it stale (covers a slow run that's still legitimately finishing).
WINDOWS_TASKS = {
    "Forex Daily Run":        ("ATOS Forex Daily Run",        "forex_scheduler.log",   20),
    "Forex Exit Check":       ("ATOS Forex Exit Check",       "forex_scheduler.log",   20),
    "Forex London Run":       ("ATOS Forex London Run",       "forex_scheduler.log",   20),
    "Forex Gap London":       ("ATOS Forex Gap London",       "forex_scheduler.log",   20),
    "Forex Gap NewYork":      ("ATOS Forex Gap NewYork",      "forex_scheduler.log",   20),
    "Forex Gap Monday Early": ("ATOS Forex Gap Monday Early", "forex_scheduler.log",   20),
    "Forex Gap Fill":         ("ATOS Forex Gap Fill",         "forex_scheduler.log",   20),
    "Futures Daily Run":      ("ATOS Futures Daily Run",      "futures_scheduler.log", 30),
    "ETF Daily Run":          ("ATOS ETF Daily Run",          "etf_scheduler.log",     20),
    "Stocks Daily Run":       ("ATOS Daily Run",              "engine_TODAY.log",      15),  # special-cased below
    "Intraday Monitor":       ("ATOS Intraday Monitor",       "intraday_monitor.log",  15),
    "PnL Sync":               ("ATOS PnL Sync",               "pnl_sync.log",          10),
}

# ── Registry: Claude-native scheduled tasks (no Windows entry) ──────────────
# name -> (log_file, [(weekday_set, hour_utc, minute_utc)], grace_minutes)
# weekday_set: 1=Mon .. 7=Sun, per Python's isoweekday(). None = every day.
CLAUDE_TASKS = {
    "LBO London Open":  ("lbo_london.log", [({1,2,3,4,5}, 7, 7)],   20),
    "LBO NY Open":      ("lbo_ny.log",     [({1,2,3,4,5}, 13, 9)],  20),
    "LBO Force Close":  ("lbo_close.log",  [(None, 20, 9)],         20),
}


def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _query_task_info(task_name: str) -> dict | None:
    """Return {'LastRunTime': datetime|None, 'LastTaskResult': int, 'Enabled': bool} or None if not found."""
    ps_cmd = (
        f"$t = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue; "
        f"if (-not $t) {{ Write-Output 'NOTFOUND'; exit }} "
        f"$i = Get-ScheduledTaskInfo -TaskName '{task_name}'; "
        f"[PSCustomObject]@{{LastRunTime=$i.LastRunTime; LastTaskResult=$i.LastTaskResult; "
        f"State=$t.State.ToString()}} | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception as exc:
        return {"error": f"powershell query failed: {exc}"}

    if not out or out == "NOTFOUND":
        return None
    try:
        data = json.loads(out)
    except Exception:
        return {"error": f"could not parse task info: {out[:200]}"}

    last_run = None
    if data.get("LastRunTime"):
        # PowerShell ConvertTo-Json emits .NET dates as "/Date(ms)/ " or ISO — handle both
        raw = data["LastRunTime"]
        if isinstance(raw, str) and raw.startswith("/Date("):
            ms = int(raw[6:raw.index(")")])
            last_run = datetime.fromtimestamp(ms / 1000)
        elif isinstance(raw, str):
            try:
                last_run = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                last_run = None

    return {
        "last_run": last_run,
        "last_result": data.get("LastTaskResult"),
        "state": data.get("State", "?"),
    }


def _log_mtime(log_file: str) -> datetime | None:
    path = os.path.join(DATA_DIR, log_file)
    if not os.path.exists(path):
        return None
    return datetime.fromtimestamp(os.path.getmtime(path))


def _check_windows_task(name: str, task_name: str, log_file: str, grace_min: int) -> str | None:
    """Return a failure description, or None if healthy."""
    if log_file == "engine_TODAY.log":
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = f"engine_{today}.log"

    info = _query_task_info(task_name)
    if info is None:
        return f"task '{task_name}' not found in Windows Task Scheduler (was it renamed or removed?)"
    if "error" in info:
        return f"could not query '{task_name}': {info['error']}"

    last_run = info["last_run"]
    result   = info["last_result"]

    if info["state"] == "Disabled":
        return None  # deliberately disabled — not a failure

    if last_run is None:
        return None  # never scheduled to run yet, nothing to check

    now = datetime.now()
    # Only judge tasks that fired recently enough that we'd expect fresh output by now.
    if now - last_run > timedelta(hours=24):
        return None  # last run too long ago to be "this check's" concern

    if result not in (0, TASK_NEVER_RUN):
        return f"'{task_name}' last run at {last_run:%Y-%m-%d %H:%M} returned error code {result}"

    mtime = _log_mtime(log_file)
    if mtime is None:
        if now - last_run > timedelta(minutes=grace_min):
            return f"'{task_name}' ran at {last_run:%Y-%m-%d %H:%M} (reported success) but {log_file} does not exist — likely silent no-op"
        return None

    if mtime < last_run - timedelta(minutes=2) and now - last_run > timedelta(minutes=grace_min):
        return (f"'{task_name}' ran at {last_run:%Y-%m-%d %H:%M} (reported success) but "
                f"{log_file} was last written {mtime:%Y-%m-%d %H:%M} — stale, likely silent no-op")

    return None


def _most_recent_expected(schedule: list, grace_min: int) -> datetime | None:
    """Given [(weekday_set_or_None, hour_utc, minute_utc), ...], find the most
    recent occurrence that's already passed its grace period, in local time."""
    now_utc = datetime.now(timezone.utc)
    candidates = []
    for weekdays, hh, mm in schedule:
        for days_back in range(8):  # look back up to a week
            cand = (now_utc - timedelta(days=days_back)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            if cand > now_utc:
                continue
            if weekdays is not None and cand.isoweekday() not in weekdays:
                continue
            if now_utc - cand < timedelta(minutes=grace_min):
                continue  # too recent, still in grace period
            candidates.append(cand)
            break
    if not candidates:
        return None
    return max(candidates).replace(tzinfo=None) + timedelta(hours=0)  # naive local-equivalent, see note below


def _check_claude_task(name: str, log_file: str, schedule: list, grace_min: int) -> str | None:
    expected = _most_recent_expected(schedule, grace_min)
    if expected is None:
        return None
    # Log mtimes are local (system) time; expected is UTC-naive here for
    # comparison purposes only relative ordering matters, not absolute tz,
    # since we just need "did anything get written since the expected fire".
    mtime = _log_mtime(log_file)
    if mtime is None:
        return f"'{name}' expected to fire around {expected:%Y-%m-%d %H:%M} UTC but {log_file} does not exist"
    # Compare using UTC-aware mtime vs expected (expected already UTC-naive)
    mtime_utc = datetime.utcfromtimestamp(os.path.getmtime(os.path.join(DATA_DIR, log_file)))
    if mtime_utc < expected - timedelta(minutes=2):
        return (f"'{name}' expected to fire around {expected:%Y-%m-%d %H:%M} UTC but "
                f"{log_file} was last written {mtime_utc:%Y-%m-%d %H:%M} UTC — likely missed or failed")
    return None


def _send_alert(failures: list[str]) -> None:
    if not os.path.exists(EMAIL_CFG):
        print("[watchdog] no config/email.json — cannot send alert, printing instead:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return
    with open(EMAIL_CFG) as f:
        cfg = json.load(f)

    now = datetime.now().strftime("%Y-%m-%d %H:%M PKT")
    rows = "".join(f"<li style='margin:6px 0'>{f}</li>" for f in failures)
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#0d1117;color:#e6edf3;padding:20px">
<div style="max-width:640px;margin:0 auto;background:#161b22;border-radius:10px;
            border-top:3px solid #da3633;padding:20px 24px">
<h2 style="color:#f85149;margin:0 0 12px">⚠ Scheduler Watchdog Alert</h2>
<div style="color:#8b949e;font-size:12px;margin-bottom:14px">{now} — {len(failures)} task(s) failed or went silent</div>
<ul style="padding-left:18px;font-size:13px">{rows}</ul>
<hr style="border:none;border-top:1px solid #21262d;margin:16px 0">
<div style="color:#484f58;font-size:11px">ATOS Scheduler Watchdog · runs every 30 min ·
check data/*.log and Task Scheduler directly for detail</div>
</div></body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[ATOS] Scheduler Alert — {len(failures)} task(s) failed"
        msg["From"]    = f"ATOS Watchdog <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        print(f"[watchdog] alert email sent for {len(failures)} failure(s)")
    except Exception as exc:
        print(f"[watchdog] ALERT EMAIL FAILED to send: {exc}", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify scheduled trading tasks actually ran")
    ap.add_argument("--verbose", action="store_true", help="print status for every task, not just failures")
    args = ap.parse_args()

    state          = _load_state()
    alerted_at     = state.get("alerted_at", {})
    failures       = []   # new alerts to actually send this run
    unhealthy      = []   # every currently-failing task, regardless of dedup
    now_iso        = datetime.now().isoformat()

    for name, (task_name, log_file, grace) in WINDOWS_TASKS.items():
        result = _check_windows_task(name, task_name, log_file, grace)
        if args.verbose:
            print(f"[{'FAIL' if result else 'ok  '}] {name}: {result or 'healthy'}")
        if result:
            unhealthy.append(name)
            last_alert = alerted_at.get(name)
            if last_alert and (datetime.now() - datetime.fromisoformat(last_alert)) < timedelta(hours=REALERT_AFTER_HOURS):
                continue  # already alerted recently, suppress repeat
            failures.append(result)
            alerted_at[name] = now_iso
        else:
            alerted_at.pop(name, None)

    for name, (log_file, schedule, grace) in CLAUDE_TASKS.items():
        result = _check_claude_task(name, log_file, schedule, grace)
        if args.verbose:
            print(f"[{'FAIL' if result else 'ok  '}] {name}: {result or 'healthy'}")
        if result:
            unhealthy.append(name)
            last_alert = alerted_at.get(name)
            if last_alert and (datetime.now() - datetime.fromisoformat(last_alert)) < timedelta(hours=REALERT_AFTER_HOURS):
                continue
            failures.append(result)
            alerted_at[name] = now_iso
        else:
            alerted_at.pop(name, None)

    state["alerted_at"]  = alerted_at
    state["last_check"]  = now_iso
    _save_state(state)

    if failures:
        _send_alert(failures)
    elif unhealthy:
        if args.verbose:
            print(f"[watchdog] {len(unhealthy)} task(s) still unhealthy but already alerted "
                  f"within the last {REALERT_AFTER_HOURS}h, not re-sending: {', '.join(unhealthy)}")
    elif args.verbose:
        print(f"[watchdog] all {len(WINDOWS_TASKS) + len(CLAUDE_TASKS)} tasks healthy at {now_iso}")


if __name__ == "__main__":
    main()
