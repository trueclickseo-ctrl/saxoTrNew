# fix_stocks_etf_futures_hourly.ps1
# ------------------------------------
# Explicit user request (2026-08-25): Stocks/ETF/Futures should each scan
# once an hour, not once a day -- and any leftover extra scan tasks for
# these 3 modules should be removed. Forex (SIM + LIVE) is untouched here;
# this script only touches the other 3.
#
# Verified before writing this: all 3 modules already combine entry AND
# exit checking in ONE pass (same pattern as forex's run_daily()) --
# atos_runner.py calls should_exit(), run_etf_bot.py's run_once() calls
# self.executor.review_exits(), futures/runner.py's run_daily() calls
# should_exit() too. So one hourly trigger per module covers both needs,
# no separate exit-check task required.
#
# Trigger construction uses the CIM-based approach (New-CimInstance for
# MSFT_TaskRepetitionPattern, assigned to a plain -Daily trigger's
# .Repetition property) -- the only one confirmed to actually work with
# PowerShell's ScheduledTasks module; see fix_intraday_scan_trigger.ps1's
# comments for the two ways this failed before landing on it.
#
# Also unregisters "ATOS ETF Test Run 1/2/3 2026-08-24" -- one-time,
# already-fired test triggers from 2026-08-24 (each StartBoundary is a
# single past date, never recurs) left over from an earlier test session.
# Harmless clutter, not an active extra scanner, but removed per "close
# rest scanner for these 3".
#
# RUN THIS ONCE, AS ADMINISTRATOR:
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\fix_stocks_etf_futures_hourly.ps1"

function Set-HourlyTrigger {
    param([string]$TaskName)
    try {
        $trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
        $rep = New-CimInstance -CimClass (Get-CimClass -ClassName MSFT_TaskRepetitionPattern -Namespace Root/Microsoft/Windows/TaskScheduler) -ClientOnly
        $rep.Interval = "PT1H"
        $rep.Duration = "PT24H"
        $rep.StopAtDurationEnd = $true
        $trigger.Repetition = $rep
        Set-ScheduledTask -TaskName $TaskName -Trigger $trigger -ErrorAction Stop | Out-Null
        Write-Host "OK   ${TaskName}: now every 1 hour, all day" -ForegroundColor Green
    } catch {
        Write-Host "FAILED ${TaskName}: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Set-HourlyTrigger -TaskName "ATOS Daily Run"          # Stocks
Set-HourlyTrigger -TaskName "ATOS ETF Daily Run"      # ETF
Set-HourlyTrigger -TaskName "ATOS Futures Daily Run"  # Futures

Write-Host ""
Write-Host "Removing stale one-time ETF test tasks (already fired, never recur)..." -ForegroundColor Cyan
foreach ($t in @("ATOS ETF Test Run 1 2026-08-24", "ATOS ETF Test Run 2 2026-08-24", "ATOS ETF Test Run 3 2026-08-24")) {
    try {
        Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction Stop
        Write-Host "OK   removed $t" -ForegroundColor Green
    } catch {
        Write-Host "FAILED to remove ${t}: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "Verify with:"
Write-Host '  Get-ScheduledTaskInfo -TaskName "ATOS Daily Run" | Select NextRunTime'
Write-Host '  Get-ScheduledTaskInfo -TaskName "ATOS ETF Daily Run" | Select NextRunTime'
Write-Host '  Get-ScheduledTaskInfo -TaskName "ATOS Futures Daily Run" | Select NextRunTime'
