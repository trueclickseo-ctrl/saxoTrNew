# fix_sim_schedule_conflicts.ps1
# --------------------------------
# Resolves wall-clock scheduling collisions between SIM's existing tasks
# and the real-money ATOS Forex LIVE schedule (2026-08-25). LIVE's times
# are the fixed reference point -- these 4 SIM tasks are shifted a few
# minutes later instead. No functional conflict existed (LIVE uses its
# own lock file/state file/Saxo account entirely, verified separately) --
# this is purely to avoid simultaneous-minute collisions.
#
# Only the TRIGGER time is changed on each task -- Action/Settings are
# left exactly as already configured (Set-ScheduledTask with just
# -Trigger touches nothing else).
#
# RUN THIS ONCE, AS ADMINISTRATOR:
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\fix_sim_schedule_conflicts.ps1"

$changes = @(
    @{ Name = "ATOS Forex Intraday Scan"; OldTime = "06:00"; NewTime = "06:05" }
    @{ Name = "ATOS Futures Discover";    OldTime = "06:00"; NewTime = "06:10" }
    @{ Name = "ATOS Forex Exit Check";    OldTime = "14:00"; NewTime = "14:05" }
    @{ Name = "ATOS Forex London Run";    OldTime = "18:00"; NewTime = "18:05" }
)

foreach ($c in $changes) {
    try {
        $newTrigger = New-ScheduledTaskTrigger -Daily -At $c.NewTime
        Set-ScheduledTask -TaskName $c.Name -Trigger $newTrigger -ErrorAction Stop | Out-Null
        Write-Host "OK   $($c.Name): $($c.OldTime) -> $($c.NewTime)" -ForegroundColor Green
    } catch {
        Write-Host "FAILED $($c.Name): $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "ATOS Forex LIVE Daily Run / Exit Check were NOT touched -- LIVE's schedule is the fixed reference point." -ForegroundColor Cyan
Write-Host ""
Write-Host "Verify with:"
Write-Host '  Get-ScheduledTask | Where-Object {$_.TaskName -like "ATOS*"} | ForEach-Object { [PSCustomObject]@{Name=$_.TaskName; Times=($_.Triggers.StartBoundary -join ", ")} }'
