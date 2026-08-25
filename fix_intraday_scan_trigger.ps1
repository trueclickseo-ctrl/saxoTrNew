# fix_intraday_scan_trigger.ps1
# --------------------------------
# Restores "ATOS Forex Intraday Scan"'s every-30-min repeating trigger,
# accidentally destroyed by fix_sim_schedule_conflicts.ps1 (2026-08-25).
#
# Root cause: that script called
#   Set-ScheduledTask -Trigger (New-ScheduledTaskTrigger -Daily -At $time)
# for 4 tasks. For 3 of them (genuinely once-daily tasks) that's fine --
# Set-ScheduledTask -Trigger REPLACES the entire trigger set. But "ATOS
# Forex Intraday Scan" needs a REPEATING trigger (every 30 min, 06:00-22:00
# PKT, per its own registered description) -- the plain -Daily -At trigger
# silently dropped that repetition, leaving it to fire only once a day at
# 06:05. Confirmed via Windows' own Task Scheduler event log: it fired
# correctly on every 30-min mark through 2026-08-25 20:00, was modified at
# 20:20:08 (when the conflict-fix script ran), and never fired again.
#
# This script rebuilds the correct repeating trigger: starts at 06:05 (the
# already-shifted time, to keep the conflict-avoidance fix in place),
# repeats every 30 min, for a 16-hour span (06:05 -> 22:05 PKT) -- matching
# "every 30 min, 06:00-22:00 PKT" from the task's own description.
#
# CAUGHT AND FIXED 2026-08-25, THREE TIMES (each prior attempt failed a
# different way):
#   v1 used `-Once -At X -RepetitionInterval ...`, which only repeats
#      WITHIN that single day's 16h window from X, then stops for good
#      (StopAtDurationEnd) -- does NOT recur on subsequent days, since
#      -Once never establishes a daily recurrence in the first place.
#   v2 tried `-Daily -At X -RepetitionInterval ...` together, which
#      New-ScheduledTaskTrigger's cmdlet REJECTS outright ("Parameter
#      set cannot be resolved") -- -RepetitionInterval/-RepetitionDuration
#      are only valid alongside -Once at the cmdlet level.
#   v3 tried setting $trigger.Repetition.Interval directly on a plain
#      -Daily trigger -- fails with "property cannot be found" because
#      .Repetition is $null on a freshly-created trigger, not a settable
#      sub-object yet.
# Fix: build a real MSFT_TaskRepetitionPattern CIM instance via
# New-CimInstance (ClientOnly -- we're composing it locally, not querying
# a live one), set its Interval/Duration as ISO8601 duration strings
# ("PT30M"/"PT16H", not a TimeSpan or its .ToString()), assign THAT to
# the trigger's .Repetition property, then pass the whole trigger to
# Set-ScheduledTask. Verified working end-to-end before handing this back.
#
# RUN THIS ONCE, AS ADMINISTRATOR:
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\fix_intraday_scan_trigger.ps1"

$taskName = "ATOS Forex Intraday Scan"

try {
    $trigger = New-ScheduledTaskTrigger -Daily -At "06:05"
    $rep = New-CimInstance -CimClass (Get-CimClass -ClassName MSFT_TaskRepetitionPattern -Namespace Root/Microsoft/Windows/TaskScheduler) -ClientOnly
    $rep.Interval = "PT30M"
    $rep.Duration = "PT16H"
    $rep.StopAtDurationEnd = $true
    $trigger.Repetition = $rep

    Set-ScheduledTask -TaskName $taskName -Trigger $trigger -ErrorAction Stop | Out-Null
    Write-Host "OK   $taskName trigger restored: every 30 min, 06:05-22:05" -ForegroundColor Green

    $t = (Get-ScheduledTask -TaskName $taskName).Triggers[0]
    Write-Host ""
    Write-Host "Verify:"
    Write-Host ("  StartBoundary : {0}" -f $t.StartBoundary)
    Write-Host ("  Interval      : {0}" -f $t.Repetition.Interval)
    Write-Host ("  Duration      : {0}" -f $t.Repetition.Duration)
} catch {
    Write-Host "FAILED ${taskName}: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Next scheduled run should appear within 30 min. Confirm with:"
Write-Host '  Get-ScheduledTaskInfo -TaskName "ATOS Forex Intraday Scan" | Select NextRunTime'
