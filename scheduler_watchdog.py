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

LBO tasks now have real Windows Task Scheduler entries (created 2026-08-21)
and are checked the same way as everything else in WINDOWS_TASKS below.
CLAUDE_TASKS is kept for any future task that genuinely has no Windows
Scheduler entry — currently empty.

Every alert includes the exact PowerShell command to fire the task manually
right now, since a missed run (e.g. the 2026-08-21 DisallowStartIfOnBatteries
incident, where 13 of 20 tasks silently refused to start on battery power)
means that day's window is gone until the next scheduled occurrence unless
someone runs it by hand.

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
# Separate state for the dedicated --only-forex watchdog run -- deliberately
# not shared with STATE_FILE, so the two watchdogs' alert-dedup windows
# can't collide or depend on each other (the whole point of running a
# second, independent one for Forex).
FOREX_STATE_FILE = os.path.join(DATA_DIR, "forex_watchdog_state.json")

# "Has not run yet" placeholder result code — not a real failure.
TASK_NEVER_RUN = 267011

# Suppress re-alerting the same task within this window once it's been
# flagged, so a still-broken task doesn't spam an email every 30 minutes —
# but it WILL alert again after this window if still unresolved.
REALERT_AFTER_HOURS = 4

# Positive "still alive" confirmation email cadence -- separate concern from
# REALERT_AFTER_HOURS above (which only throttles failure alerts). Added
# 2026-08-26 at the user's explicit request: they had no email confirming
# LIVE forex (or SIM ETF/Futures/Stocks) actually ran successfully, only
# ever silence-when-fine or an alert-on-failure. 4h matches the existing
# realert cadence -- frequent enough to notice a multi-hour outage, not so
# frequent it's just a second alert stream.
HEARTBEAT_EVERY_HOURS = 4

# ── Registry: Windows Task Scheduler-backed tasks ────────────────────────────
# name              -> (task_name_in_windows, log_file, grace_minutes, max_first_run_wait_hours)
# grace_minutes: how long after LastRunTime the log is allowed to lag before
# we call it stale (covers a slow run that's still legitimately finishing).
# max_first_run_wait_hours: for a task that has NEVER run yet (LastRunTime is
# the "has not run" sentinel), how long is it allowed to wait before its
# NextRunTime looking further out than this is itself treated as a failure.
# Without this, a task misconfigured to fire far less often than intended
# (e.g. weekly when it should be daily — exactly what happened to PnL Sync,
# 2026-08-21) is indistinguishable from "legitimately new, hasn't had its
# first chance yet" and the watchdog stays silent on it forever. 30h covers
# daily tasks with buffer; 78h covers weekday-only tasks over a weekend gap;
# 174h covers the two genuinely-weekly tasks (7 days + buffer).
WINDOWS_TASKS = {
    # Added 2026-08-25 after this exact task went silently once-daily for
    # ~2.5h with zero alert -- it was simply never in this registry before,
    # a coverage gap unrelated to any detection-logic bug (the OTHER forex
    # tasks below were always covered correctly). Root cause of that
    # incident: fix_sim_schedule_conflicts.ps1's Set-ScheduledTask -Trigger
    # call replaced this task's every-30-min repeating trigger with a bare
    # once-daily one (see that script's own updated comments). grace=45
    # (tolerant of normal jitter around a 30-min cadence, catches a missed
    # cycle within one watchdog pass) and max_first_run_wait=2h (tight,
    # since this task should never go more than 30 min without having run
    # under normal operation).
    "Forex Intraday Scan":    ("ATOS Forex Intraday Scan",    "forex_scheduler.log",   45, 2),
    "Forex Daily Run":        ("ATOS Forex Daily Run",        "forex_scheduler.log",   20, 30),
    "Forex Exit Check":       ("ATOS Forex Exit Check",       "forex_scheduler.log",   20, 30),
    "Forex London Run":       ("ATOS Forex London Run",       "forex_scheduler.log",   20, 30),
    # task_name repointed 2026-08-26: "ATOS Forex Gap London" was a disabled,
    # superseded duplicate of "ATOS Forex Gap London Fixed" -- deleted during
    # the same-day scheduled-task cleanup. This registry entry watches the
    # surviving real task now.
    "Forex Gap London":       ("ATOS Forex Gap London Fixed", "forex_scheduler.log",   20, 78),
    "Forex Gap NewYork":      ("ATOS Forex Gap NewYork",      "forex_scheduler.log",   20, 78),
    # Added 2026-08-26: closes a real coverage gap -- the Tokyo gap
    # session (00:00-01:30 UTC / 05:00-06:30 PKT, Tue-Fri) had no
    # dedicated task at all and fell almost entirely inside the regular
    # scan schedule's own dead zone. See fix_gap_tokyo_coverage.ps1.
    "Forex Gap Tokyo":        ("ATOS Forex Gap Tokyo",        "forex_scheduler.log",   20, 78),
    "Forex Gap Monday Early": ("ATOS Forex Gap Monday Early", "forex_scheduler.log",   20, 174),
    "Forex Gap Fill":         ("ATOS Forex Gap Fill",         "forex_scheduler.log",   20, 174),
    # max_first_run_wait tightened 30h -> 2h 2026-08-25: these 3 moved from
    # once/day to hourly (explicit user request -- "no need every minute"
    # but also no need to wait a whole day; Stocks/ETF/Futures all already
    # combine entry AND exit checking in one pass, same as forex) and were
    # added to INTRADAY_REPEATING_TASKS above so the "hasn't advanced
    # recently enough" check actually watches them now.
    "Futures Daily Run":      ("ATOS Futures Daily Run",      "futures_scheduler.log", 30, 2),
    "ETF Daily Run":          ("ATOS ETF Daily Run",          "etf_scheduler.log",     20, 2),
    "Stocks Daily Run":       ("ATOS Daily Run",              "engine_TODAY.log",      15, 2),  # special-cased below
    # log_file fixed 2026-08-25: this was pointed at data/intraday_monitor.log,
    # a dead file only a crash traceback ever touches -- the script's real
    # per-invocation output moved to logs/monitor_{date}.log at some point,
    # silently orphaning this freshness check the whole time (see _log_path's
    # updated docstring). "logs/monitor_TODAY.log" is a sentinel _check_windows_task
    # substitutes with today's real date, same pattern as "engine_TODAY.log".
    #
    # task_name repointed 2026-08-26: "ATOS Intraday Monitor" (the name this
    # registry always used) turned out to be a vestigial duplicate that only
    # fires once/week -- the task ACTUALLY doing real intraday stop-loss
    # monitoring every 1 min, 18h/day, is a differently-named legacy task,
    # "SaxoTr Intraday Monitor" (predates the "ATOS" naming convention, never
    # renamed). Both run the identical intraday_monitor.py, so the log path
    # doesn't change -- only which task's LastRunTime/result this watchdog
    # actually checks. grace/max_wait tightened to match its real 1-min
    # cadence (was tuned for the vestigial weekly one) and added to
    # INTRADAY_REPEATING_TASKS below so a silent multi-hour outage on this
    # one gets caught, not just "ran once sometime this week."
    "Intraday Monitor":       ("SaxoTr Intraday Monitor",     "logs/monitor_TODAY.log", 10, 1),
    "PnL Sync":               ("ATOS PnL Sync",               "pnl_sync.log",          10, 30),
    "LBO London Open":        ("ATOS LBO London Open",        "lbo_london.log",        20, 78),
    "LBO NY Open":             ("ATOS LBO NY Open",            "lbo_ny.log",            20, 78),
    "LBO Force Close":        ("ATOS LBO Force Close",        "lbo_close.log",         20, 30),
    "Daily Chart":            ("ATOS Daily Chart",            "daily_chart_scheduler.log", 15, 30),
    # Real-money LIVE forex account (2026-08-25) -- separate log from SIM's
    # forex_scheduler.log so a LIVE failure is never masked by SIM's own
    # (much more frequent) log activity. Daily Run moved from once/day to
    # 9x/day the same evening (3 scans per FX session) -- max_first_run_wait
    # tightened to 4h (was 30h) so a degraded task silently firing only
    # its last remaining trigger (~24h gaps) gets caught quickly instead of
    # looking like a normal daily cadence. Exit Check stays once/day.
    "Forex LIVE Daily Run":   ("ATOS Forex LIVE Daily Run",   "forex_live_scheduler.log", 30, 4),
    "Forex LIVE Exit Check":  ("ATOS Forex LIVE Exit Check",  "forex_live_scheduler.log", 30, 30),
    # Second real-money account -- EUR sub-account (2026-08-26), RSI
    # Pullback only, on the same 17-pair HIGH_VOLUME_SYMBOLS universe as
    # the SEK account as of 2026-08-28. Own log, own state, own
    # SAXO_LIVE_EUR_CONFIRMED flag -- genuinely separate from the SEK
    # account above, so a failure on one is never masked by the other's
    # activity. Same grace/max_wait rationale as the SEK entries.
    "Forex LIVE EUR Daily Run":  ("ATOS Forex LIVE EUR Daily Run",  "forex_live_eur_scheduler.log", 30, 4),
    "Forex LIVE EUR Exit Check": ("ATOS Forex LIVE EUR Exit Check", "forex_live_eur_scheduler.log", 30, 30),
    # Added 2026-08-25 alongside saxo_live_token_keepalive.py itself (see
    # that file's docstring for why it exists: LIVE's refresh_token only
    # lives 1h, LIVE's trading runs are ~2h apart, so something has to
    # touch the token more often than that or every run in between fails
    # with TOKEN EXPIRED). grace=20 (well under its own 15-min cadence),
    # max_first_run_wait=1 (tight -- this task should never go an hour
    # without having run at least once under normal operation).
    "Saxo LIVE Token Keepalive": ("ATOS Saxo LIVE Token Keepalive", "saxo_live_keepalive.log", 20, 1),
}

# ── Registry: Claude-native scheduled tasks (no Windows entry) ──────────────
# name -> (log_file, [(weekday_set, hour_utc, minute_utc)], grace_minutes)
# weekday_set: 1=Mon .. 7=Sun, per Python's isoweekday(). None = every day.
# Currently empty — every task has a real Windows Task Scheduler entry now.
CLAUDE_TASKS = {}


def _load_state(state_file: str = STATE_FILE) -> dict:
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state: dict, state_file: str = STATE_FILE) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = state_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, state_file)


def _parse_ps_date(raw) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    if raw.startswith("/Date("):
        ms = int(raw[6:raw.index(")")])
        return datetime.fromtimestamp(ms / 1000)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _query_task_info(task_name: str) -> dict | None:
    """Return {'LastRunTime': datetime|None, 'NextRunTime': datetime|None,
    'LastTaskResult': int, 'Enabled': bool} or None if not found."""
    ps_cmd = (
        f"$t = Get-ScheduledTask -TaskName '{task_name}' -ErrorAction SilentlyContinue; "
        f"if (-not $t) {{ Write-Output 'NOTFOUND'; exit }} "
        f"$i = Get-ScheduledTaskInfo -TaskName '{task_name}'; "
        f"[PSCustomObject]@{{LastRunTime=$i.LastRunTime; NextRunTime=$i.NextRunTime; "
        f"LastTaskResult=$i.LastTaskResult; State=$t.State.ToString()}} | ConvertTo-Json -Compress"
    )
    # Retry once on a transient PowerShell/WMI stall before reporting a
    # failure. Confirmed live 2026-08-26: "ATOS Daily Chart" alerted with
    # "timed out after -3474.9370000000017 seconds" -- a negative timeout
    # is not something subprocess.run() was ever asked for (timeout=30 is
    # a fixed literal below); CPython's Popen.wait() recomputes the
    # *remaining* time on each poll and raises TimeoutExpired with that
    # recomputed (occasionally negative, if the system clock jumps forward
    # mid-wait -- e.g. a sleep/resume or large NTP correction) value
    # instead of the original 30. The task itself was healthy the whole
    # time (LastTaskResult 0, fresh log) -- only this one query call
    # glitched. A single retry costs nothing on the transient case and
    # avoids a spurious alert email over a one-off environmental blip.
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=30,
                # CREATE_NO_WINDOW: without this, every one of these per-task
                # subprocess calls pops its own visible console window on
                # Windows, even though scheduler_watchdog.py itself runs
                # invisibly (pythonw via run_hidden.vbs) -- the parent process
                # having no window doesn't stop a CHILD process from getting
                # its own. Found 2026-08-26: with 20 tasks in WINDOWS_TASKS
                # (13 of them forex/LBO-named, checked again by the separate
                # "ATOS Forex Watchdog" --only-forex task), each watchdog run
                # was popping 13-20 empty PowerShell windows in rapid
                # succession -- purely cosmetic (each closes itself instantly),
                # but visually disruptive. subprocess.CREATE_NO_WINDOW only
                # exists on Windows, which this entire module already targets
                # exclusively (Task Scheduler, schtasks, PowerShell).
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout.strip()
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
    if last_exc is not None:
        return {"error": f"powershell query failed after retry: {last_exc}"}

    if not out or out == "NOTFOUND":
        return None
    try:
        data = json.loads(out)
    except Exception:
        return {"error": f"could not parse task info: {out[:200]}"}

    return {
        "last_run": _parse_ps_date(data.get("LastRunTime")),
        "next_run": _parse_ps_date(data.get("NextRunTime")),
        "last_result": data.get("LastTaskResult"),
        "state": data.get("State", "?"),
    }


def _log_path(log_file: str) -> str | None:
    """Resolve the actual log path for a task, preferring the primary path
    but falling back to the ".fallback" sibling run_hidden.vbs writes to
    when the primary is persistently locked and it had to route around it
    (see run_hidden.vbs) -- whichever exists and is newer wins, so the
    watchdog doesn't go blind to real output that landed in the fallback.

    A log_file containing a path separator (e.g. "logs/monitor_TODAY.log")
    resolves relative to BASE_DIR instead of DATA_DIR -- added 2026-08-25
    after finding "Intraday Monitor"'s registry entry had been silently
    checking data/intraday_monitor.log (a dead file, only ever touched by
    a crash traceback) for who knows how long, while the script's real
    output moved to logs/monitor_{date}.log at some point without this
    registry entry being updated to match. Every other bare-filename entry
    is unaffected -- still resolved under DATA_DIR exactly as before."""
    if "/" in log_file or os.sep in log_file:
        primary = os.path.join(BASE_DIR, log_file)
    else:
        primary = os.path.join(DATA_DIR, log_file)
    fallback = primary + ".fallback"
    have_primary  = os.path.exists(primary)
    have_fallback = os.path.exists(fallback)
    if have_primary and have_fallback:
        return fallback if os.path.getmtime(fallback) > os.path.getmtime(primary) else primary
    if have_fallback:
        return fallback
    if have_primary:
        return primary
    return None


def _log_mtime(log_file: str) -> datetime | None:
    path = _log_path(log_file)
    if path is None:
        return None
    return datetime.fromtimestamp(os.path.getmtime(path))


# Signatures of a run that "succeeded" by Windows' own accounting (exit 0)
# and touched its log file at the right time (passes the freshness check
# below) but never actually did anything -- confirmed live 2026-08-21/22:
# the futures scheduler's run_hidden.vbs wrapper touches data/futures_
# scheduler.log via its ">>" redirect at exactly the scheduled time even
# when the redirect itself fails to open the file (another process still
# holding it), so the file's mtime looks perfectly healthy while its only
# content is this one Windows shell error line. mtime freshness alone
# cannot catch this class of failure -- it needs to look at what actually
# got written.
_FAILURE_SIGNATURES = (
    "cannot access the file",       # Windows sharing violation on the log redirect itself
    "is not recognized as an internal or external command",
    "Traceback (most recent call last):",
    "ModuleNotFoundError",
    "ImportError",
)
# A log with real run output is normally at least a few hundred bytes
# (banner, per-symbol scan lines, summary). Anything this small combined
# with a failure signature is almost certainly a stub, not a real run.
_SUSPICIOUSLY_SMALL_BYTES = 200


# Tasks that genuinely fire multiple times within a single day -- the ONLY
# ones the "hasn't advanced recently enough" check below should apply to.
# grace_min alone is NOT a reliable proxy for this: several genuinely
# once-daily tasks (Forex Exit Check, PnL Sync, the LBO session-open
# tasks) also use a tight grace_min for unrelated reasons (how long their
# OWN single run is allowed to take to produce output), and briefly
# false-alarmed on all four the same day this set was added, once as
# "hasn't fired in N hours" purely because their one daily run was hours
# in the past and their own NextRunTime (tomorrow, same time) looked more
# than 12h out -- completely normal for a once-daily task, not a failure.
INTRADAY_REPEATING_TASKS = {
    "Forex Intraday Scan", "Forex LIVE Daily Run", "Forex LIVE EUR Daily Run",
    "Saxo LIVE Token Keepalive",
    "Stocks Daily Run", "ETF Daily Run", "Futures Daily Run", "Intraday Monitor",
}


def _log_content_failure(log_file: str) -> str | None:
    """Return a description if the log's own content indicates a no-op/crash
    that mtime freshness alone wouldn't catch, else None."""
    path = _log_path(log_file)
    if path is None:
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            tail = f.read().decode("utf-8", errors="replace")
    except Exception:
        return None  # can't read it (e.g. still locked) -- not this check's job
    if size < _SUSPICIOUSLY_SMALL_BYTES:
        for sig in _FAILURE_SIGNATURES:
            if sig in tail:
                return (f"{log_file} is only {size} bytes and contains "
                        f"\"{sig}\" -- looks like a stub/crash, not real run output")
    return None


def _remediation(task_name: str) -> str:
    return f"Run manually: Start-ScheduledTask -TaskName \"{task_name}\""


def _check_windows_task(name: str, task_name: str, log_file: str, grace_min: int,
                        max_first_run_wait_hours: int = 30, info_out: dict | None = None) -> str | None:
    """Return a failure description (with a manual-fire remediation command), or None if healthy.

    info_out, if given, gets this task's raw _query_task_info() result stashed
    under `name` whenever the query itself succeeded -- lets main() build the
    periodic "still alive" heartbeat email (see _send_heartbeat) from the same
    query this function already had to make, instead of re-querying Task
    Scheduler a second time just to report status the caller already has."""
    if log_file == "engine_TODAY.log":
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = f"engine_{today}.log"
    elif log_file == "logs/monitor_TODAY.log":
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = f"logs/monitor_{today}.log"

    info = _query_task_info(task_name)
    if info is None:
        return f"task '{task_name}' not found in Windows Task Scheduler (was it renamed or removed?)"
    if "error" in info:
        return f"could not query '{task_name}': {info['error']}"
    if info_out is not None:
        info_out[name] = info

    last_run = info["last_run"]
    next_run = info.get("next_run")
    result   = info["last_result"]

    if info["state"] == "Disabled":
        return None  # deliberately disabled — not a failure

    now = datetime.now()

    if last_run is None:
        # Never run yet — legitimate for a brand-new task, but if its own
        # NextRunTime is further out than this task is supposed to tolerate,
        # the trigger itself is very likely misconfigured (e.g. weekly when
        # it should be daily) rather than genuinely "hasn't had its first
        # chance yet." That combination silently hid the PnL Sync bug.
        if next_run and next_run - now > timedelta(hours=max_first_run_wait_hours):
            return (f"'{task_name}' has never run and its own NextRunTime "
                    f"({next_run:%Y-%m-%d %H:%M}) is more than {max_first_run_wait_hours}h "
                    f"away — the trigger is very likely misconfigured (wrong recurrence, e.g. "
                    f"weekly instead of daily). {_remediation(task_name)}")
        return None
    # Only judge tasks that fired recently enough that we'd expect fresh output by now.
    if now - last_run > timedelta(hours=24):
        return None  # last run too long ago to be "this check's" concern

    # A task with a tight grace window is expected to repeat frequently
    # (e.g. every 30 min) -- everything above this point only checks
    # whether the log/result were CONSISTENT with that one specific
    # last_run, never whether last_run itself is recent enough given how
    # often the task is supposed to fire. Found live 2026-08-25: "ATOS
    # Forex Intraday Scan"'s every-30-min trigger got silently replaced
    # with a once-daily one by an unrelated schedule-conflict fix, and
    # every check below this line kept reporting "healthy" for 2.5+ hours
    # because last_run/the log were perfectly self-consistent for that one
    # (increasingly stale) run -- nothing compared last_run against "now"
    # directly. Only applies to tasks in INTRADAY_REPEATING_TASKS (see that
    # set's own comment) -- grace_min alone is NOT a safe proxy for "fires
    # multiple times a day": several genuinely once-daily tasks (Forex
    # Exit Check, PnL Sync, the LBO tasks) also use a tight grace_min for
    # unrelated reasons and briefly false-alarmed here the same day this
    # was first tried with a "grace_min <= 60" condition instead.
    #
    # A repeating intraday task also legitimately goes quiet overnight
    # (e.g. 22:05 -> next day 06:05, ~8h) as part of its own intended
    # window -- that gap alone must not trip this check, or it fires a
    # false alarm every single night. Found live 2026-08-25 fixing this
    # exact check: right after correctly restoring "ATOS Forex Intraday
    # Scan"'s trigger, this flagged it as broken again purely because
    # last_run (today's final 20:00 firing) was hours behind "now" (23:3x)
    # -- even though NextRunTime correctly showed tomorrow 06:05, proving
    # the trigger was genuinely healthy. Only escalate when NextRunTime
    # ALSO looks unhealthy (missing entirely, or absurdly far out) --
    # exactly the signature a truly broken/disabled trigger leaves, and
    # exactly what "-Once" (never recurs) produced when this was actually
    # broken: a blank NextRunTime, not just a next-day one.
    if name in INTRADAY_REPEATING_TASKS and now - last_run > timedelta(minutes=grace_min * 3):
        next_run_looks_healthy = next_run and next_run - now <= timedelta(hours=12)
        if not next_run_looks_healthy:
            mins_ago = (now - last_run).total_seconds() / 60
            next_desc = f"{next_run:%Y-%m-%d %H:%M}" if next_run else "none scheduled"
            return (f"'{task_name}' hasn't fired since {last_run:%Y-%m-%d %H:%M} "
                    f"({mins_ago:.0f} min ago) -- its own {grace_min}-min grace window implies it's "
                    f"supposed to repeat far more often than that, and its NextRunTime ({next_desc}) "
                    f"doesn't look like a healthy near-term occurrence either. Its trigger may have "
                    f"been silently replaced or disabled. {_remediation(task_name)}")

    if result not in (0, TASK_NEVER_RUN):
        # A non-zero/unrecognized result code can be transient — Task Scheduler
        # sometimes reports an in-progress/finalizing code (e.g. 267009) if
        # queried in the few seconds right after a run starts, before it has
        # settled to a final 0. Trust the log over the code: if the log is
        # fresh (proves the run genuinely completed), don't false-alarm on a
        # code that was just caught mid-transition. Only escalate if the log
        # is ALSO stale/missing — that combination is a real failure.
        mtime = _log_mtime(log_file)
        if mtime and mtime >= last_run - timedelta(minutes=2):
            return None
        return (f"'{task_name}' last run at {last_run:%Y-%m-%d %H:%M} returned error code {result} "
                f"and {log_file} isn't fresh either. {_remediation(task_name)}")

    mtime = _log_mtime(log_file)
    if mtime is None:
        if now - last_run > timedelta(minutes=grace_min):
            return (f"'{task_name}' ran at {last_run:%Y-%m-%d %H:%M} (reported success) but {log_file} "
                    f"does not exist — likely silent no-op. {_remediation(task_name)}")
        return None

    if mtime < last_run - timedelta(minutes=2) and now - last_run > timedelta(minutes=grace_min):
        return (f"'{task_name}' ran at {last_run:%Y-%m-%d %H:%M} (reported success) but "
                f"{log_file} was last written {mtime:%Y-%m-%d %H:%M} — stale, likely silent no-op. "
                f"{_remediation(task_name)}")

    # mtime alone says "fresh" here, but a wrapper script can touch the log
    # file (via its own >> redirect) at exactly the right time even when the
    # real command inside never ran or crashed immediately -- confirmed
    # live: the futures log was touched to the second at the scheduled time
    # every day for 3 days while containing only a one-line Windows sharing-
    # violation error. Check the content, not just the timestamp.
    content_issue = _log_content_failure(log_file)
    if content_issue:
        return (f"'{task_name}' ran at {last_run:%Y-%m-%d %H:%M} (reported success, log looks "
                f"fresh) but {content_issue}. {_remediation(task_name)}")

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


def _send_alert(failures: list[str], label: str = "Scheduler Watchdog") -> None:
    if not os.path.exists(EMAIL_CFG):
        print(f"[{label}] no config/email.json — cannot send alert, printing instead:", file=sys.stderr)
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
<h2 style="color:#f85149;margin:0 0 12px">⚠ {label} Alert</h2>
<div style="color:#8b949e;font-size:12px;margin-bottom:14px">{now} — {len(failures)} task(s) failed or went silent</div>
<ul style="padding-left:18px;font-size:13px">{rows}</ul>
<hr style="border:none;border-top:1px solid #21262d;margin:16px 0">
<div style="color:#484f58;font-size:11px">ATOS {label} · runs every 30 min ·
check data/*.log and Task Scheduler directly for detail</div>
</div></body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[ATOS] {label} — {len(failures)} task(s) failed"
        msg["From"]    = f"ATOS {label} <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        print(f"[{label}] alert email sent for {len(failures)} failure(s)")
    except Exception as exc:
        print(f"[{label}] ALERT EMAIL FAILED to send: {exc}", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)


def _send_heartbeat(task_info: dict, unhealthy: list[str], label: str = "Scheduler Watchdog") -> None:
    """Positive "still alive" confirmation -- separate from _send_alert, which
    only ever fires on failure. Added 2026-08-26: the user has no way to tell
    "silence because everything's fine" apart from "silence because an alert
    got swallowed somewhere" -- explicitly asked for confirmation that LIVE
    forex (and SIM ETF/Futures/Stocks) actually ran, not just failure alerts.
    Sent on a fixed cadence (HEARTBEAT_EVERY_HOURS, tracked per state file so
    the main and --only-forex watchdogs each keep their own schedule) rather
    than every 30-min run, which would just be a second flavor of spam."""
    if not os.path.exists(EMAIL_CFG):
        return
    with open(EMAIL_CFG) as f:
        cfg = json.load(f)

    now = datetime.now().strftime("%Y-%m-%d %H:%M PKT")
    rows = ""
    for name, info in sorted(task_info.items()):
        ok = name not in unhealthy
        color = "#2ea043" if ok else "#da3633"
        status = "OK" if ok else "UNHEALTHY (see last alert)"
        last_run_s = f"{info['last_run']:%Y-%m-%d %H:%M}" if info.get("last_run") else "never"
        rows += (f"<li style='margin:4px 0'><span style='color:{color};font-weight:bold'>{status}</span>"
                 f" &mdash; {name}  (last ran {last_run_s})</li>")

    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,sans-serif;background:#0d1117;color:#e6edf3;padding:20px">
<div style="max-width:640px;margin:0 auto;background:#161b22;border-radius:10px;
            border-top:3px solid #2ea043;padding:20px 24px">
<h2 style="color:#3fb950;margin:0 0 12px">✓ {label} Heartbeat</h2>
<div style="color:#8b949e;font-size:12px;margin-bottom:14px">{now} — {len(task_info)} task(s) checked,
{len(unhealthy)} unhealthy</div>
<ul style="padding-left:8px;font-size:13px;list-style:none">{rows}</ul>
<hr style="border:none;border-top:1px solid #21262d;margin:16px 0">
<div style="color:#484f58;font-size:11px">ATOS {label} · periodic confirmation the scans are actually
running (not just silence-means-fine) · sent every {HEARTBEAT_EVERY_HOURS}h</div>
</div></body></html>"""

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"[ATOS] {label} — heartbeat, {len(task_info) - len(unhealthy)}/{len(task_info)} healthy"
        msg["From"]    = f"ATOS {label} <{cfg['sender_email']}>"
        msg["To"]      = cfg["recipient_email"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            s.starttls()
            s.login(cfg["sender_email"], cfg["sender_password"])
            s.sendmail(cfg["sender_email"], cfg["recipient_email"], msg.as_string())
        print(f"[{label}] heartbeat email sent ({len(task_info) - len(unhealthy)}/{len(task_info)} healthy)")
    except Exception as exc:
        print(f"[{label}] HEARTBEAT EMAIL FAILED to send: {exc}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify scheduled trading tasks actually ran")
    ap.add_argument("--verbose", action="store_true", help="print status for every task, not just failures")
    ap.add_argument("--only-forex", action="store_true",
                     help="check only Forex/LBO tasks -- for the dedicated second Forex "
                          "watchdog (ATOS Forex Watchdog task), a deliberately redundant, "
                          "independently-scheduled check so Forex (the highest-volume "
                          "module) is never left unmonitored by a single watchdog failure "
                          "alone. Uses its own state file (data/forex_watchdog_state.json) "
                          "so its alert-dedup can't collide with or depend on the main "
                          "watchdog's state.")
    args = ap.parse_args()

    tasks = WINDOWS_TASKS
    state_file = STATE_FILE
    if args.only_forex:
        tasks = {n: v for n, v in WINDOWS_TASKS.items()
                  if n.startswith("Forex") or n.startswith("LBO")}
        state_file = FOREX_STATE_FILE

    state          = _load_state(state_file)
    alerted_at     = state.get("alerted_at", {})
    failures       = []   # new alerts to actually send this run
    unhealthy      = []   # every currently-failing task, regardless of dedup
    task_info      = {}   # name -> raw _query_task_info() result, for the heartbeat email
    now_iso        = datetime.now().isoformat()

    for name, (task_name, log_file, grace, max_wait) in tasks.items():
        result = _check_windows_task(name, task_name, log_file, grace, max_wait, info_out=task_info)
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

    if not args.only_forex:
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

    label = "Forex Watchdog" if args.only_forex else "Scheduler Watchdog"
    if failures:
        _send_alert(failures, label)
    elif unhealthy:
        if args.verbose:
            print(f"[{label}] {len(unhealthy)} task(s) still unhealthy but already alerted "
                  f"within the last {REALERT_AFTER_HOURS}h, not re-sending: {', '.join(unhealthy)}")
    elif args.verbose:
        print(f"[{label}] all {len(tasks) + (0 if args.only_forex else len(CLAUDE_TASKS))} tasks healthy at {now_iso}")

    # Heartbeat: positive confirmation the scans actually ran, independent of
    # whether anything failed -- see _send_heartbeat's docstring. Due when
    # never sent before, or HEARTBEAT_EVERY_HOURS have passed since the last one.
    last_heartbeat = state.get("last_heartbeat")
    heartbeat_due = (last_heartbeat is None or
                     (datetime.now() - datetime.fromisoformat(last_heartbeat)) >= timedelta(hours=HEARTBEAT_EVERY_HOURS))
    if heartbeat_due and task_info:
        _send_heartbeat(task_info, unhealthy, label)
        state["last_heartbeat"] = now_iso

    state["alerted_at"]  = alerted_at
    state["last_check"]  = now_iso
    _save_state(state, state_file)


if __name__ == "__main__":
    main()
