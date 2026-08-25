# setup_scheduler_live.ps1
# -------------------------
# Registers the Windows Scheduled Tasks for the real-money Saxo LIVE
# forex account (2026-08-25).
#
# 2026-08-25 (later same day): entries moved from once/day to 9x/day --
# explicit user request, 3 scan times within each of the 3 FX sessions
# (Asian/London/NY-overlap), to catch a signal as it develops through the
# day on "today's still-forming candle" rather than only checking once.
#
# 2026-08-25 (later still, same day): moved from 9 fixed times to a real
# repeating trigger -- every 45 min, 06:00-22:00 PKT (~22 runs/day, up
# from 9) -- explicit user request for tighter checking.
#
# Trigger construction went through 3 failed attempts before landing on
# the working one (same story as fix_intraday_scan_trigger.ps1, see that
# file's comments for the full account): New-ScheduledTaskTrigger's
# -RepetitionInterval/-RepetitionDuration params are ONLY valid alongside
# -Once (which then never recurs daily at all), and simply assigning
# .Repetition.Interval on a plain -Daily trigger fails ("property cannot
# be found" -- .Repetition is $null until given a real CIM instance).
# The working approach: build a real MSFT_TaskRepetitionPattern via
# New-CimInstance, set Interval/Duration as ISO8601 strings ("PT45M"/
# "PT16H"), assign that to the -Daily trigger's .Repetition property.
#
# Each scan still checks all 34 core pairs (not session-filtered).
#
# RUN THIS ONCE, AS ADMINISTRATOR (re-run any time the schedule changes --
# -Force overwrites the existing task's triggers cleanly):
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_live.ps1"
#
# IMPORTANT: these tasks will run "python forex\runner.py --account live
# --strategy donchian,ema,rsi --live" but will NOT place any real order
# until SAXO_LIVE_CONFIRMED=1 is set as a system/user environment variable
# -- that is a deliberate, separate switch (see forex/runner.py's hard
# rails). Registering these tasks does NOT by itself start real trading.

$vbs  = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base = "E:\SaxoTrNew\SaxoTrNew"
$log  = "$base\data\forex_live_scheduler.log"

$action1 = New-ScheduledTaskAction -Execute "wscript.exe" `
           -Argument "`"$vbs`" `"$base\run_forex_live_daily.bat`" `"$log`""
$trigger1 = New-ScheduledTaskTrigger -Daily -At "06:00"
$rep1 = New-CimInstance -CimClass (Get-CimClass -ClassName MSFT_TaskRepetitionPattern -Namespace Root/Microsoft/Windows/TaskScheduler) -ClientOnly
$rep1.Interval = "PT45M"
$rep1.Duration = "PT16H"
$rep1.StopAtDurationEnd = $true
$trigger1.Repetition = $rep1
$settings1 = New-ScheduledTaskSettingsSet `
           -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
           -StartWhenAvailable `
           -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

$dailyRunOk = $true
try {
    Register-ScheduledTask -TaskName "ATOS Forex LIVE Daily Run" `
               -Action $action1 -Trigger $trigger1 -Settings $settings1 `
               -Description "Real-money Saxo LIVE forex account -- donchian/ema/rsi, 34 core pairs, every 45 min 06:00-22:00 PKT" `
               -RunLevel Highest -Force -ErrorAction Stop | Out-Null
} catch {
    $dailyRunOk = $false
    Write-Host "FAILED to register 'ATOS Forex LIVE Daily Run': $($_.Exception.Message)" -ForegroundColor Red
}

$action2  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument "`"$vbs`" `"$base\run_forex_live_exits.bat`" `"$log`""
$trigger2 = New-ScheduledTaskTrigger -Daily -At "14:00"
$settings2 = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

$exitCheckOk = $true
try {
    Register-ScheduledTask -TaskName "ATOS Forex LIVE Exit Check" `
                -Action $action2 -Trigger $trigger2 -Settings $settings2 `
                -Description "Real-money Saxo LIVE forex account -- stop/time-stop check only, once daily" `
                -RunLevel Highest -Force -ErrorAction Stop | Out-Null
} catch {
    $exitCheckOk = $false
    Write-Host "FAILED to register 'ATOS Forex LIVE Exit Check': $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
if ($dailyRunOk) {
    Write-Host "Registered 'ATOS Forex LIVE Daily Run' -> every 45 min, 06:00-22:00 PKT (entries+exits)." -ForegroundColor Green
}
if ($exitCheckOk) {
    Write-Host "Registered 'ATOS Forex LIVE Exit Check' -> daily at 14:00 local (exits only, backstop)." -ForegroundColor Green
}
Write-Host ""
Write-Host "These will NOT place any real order until SAXO_LIVE_CONFIRMED=1 is set" -ForegroundColor Yellow
Write-Host "as an environment variable visible to Task Scheduler (System or User scope)." -ForegroundColor Yellow
Write-Host "Set it only when you are ready for real orders to start placing automatically:" -ForegroundColor Yellow
Write-Host '  [System.Environment]::SetEnvironmentVariable("SAXO_LIVE_CONFIRMED","1","User")' -ForegroundColor Yellow
Write-Host ""
Write-Host "Remove either task later with:"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Forex LIVE Daily Run' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Forex LIVE Exit Check' -Confirm:`$false"
