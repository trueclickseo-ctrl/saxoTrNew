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
# RUN THIS ONCE, AS ADMINISTRATOR:
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\fix_intraday_scan_trigger.ps1"

$taskName = "ATOS Forex Intraday Scan"

try {
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date -Hour 6 -Minute 5 -Second 0) `
        -RepetitionInterval (New-TimeSpan -Minutes 30) `
        -RepetitionDuration (New-TimeSpan -Hours 16)

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
