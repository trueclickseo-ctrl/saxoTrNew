# setup_scheduler_live.ps1
# -------------------------
# Registers the Windows Scheduled Tasks for the real-money Saxo LIVE
# forex account (2026-08-25).
#
# 2026-08-25 (later same day): entries moved from once/day to 9x/day --
# explicit user request, 3 scan times within each of the 3 FX sessions
# (Asian/London/NY-overlap), to catch a signal as it develops through the
# day on "today's still-forming candle" rather than only checking once.
# Each scan still checks all 34 core pairs (not session-filtered) -- the
# session labels below are just for WHEN each trigger fires, matched to
# when that session's pairs are typically most liquid.
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

# 9 daily entry-scan times -- 3 per session window:
#   Asian   (JPY/AUD/NZD most liquid): 06:00, 08:00, 10:00
#   London  (EUR/GBP/CHF/Scandi most liquid): 12:30, 14:30, 16:30
#   NY/overlap (deepest liquidity overall): 18:00 (original slot), 20:00, 22:00
$entryTimes = @("06:00", "08:00", "10:00", "12:30", "14:30", "16:30", "18:00", "20:00", "22:00")

$action1 = New-ScheduledTaskAction -Execute "wscript.exe" `
           -Argument "`"$vbs`" `"$base\run_forex_live_daily.bat`" `"$log`""
$triggers1 = foreach ($t in $entryTimes) { New-ScheduledTaskTrigger -Daily -At $t }
$settings1 = New-ScheduledTaskSettingsSet `
           -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
           -StartWhenAvailable `
           -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "ATOS Forex LIVE Daily Run" `
           -Action $action1 -Trigger $triggers1 -Settings $settings1 `
           -Description "Real-money Saxo LIVE forex account -- donchian/ema/rsi, 34 core pairs, 9x/day entries (3 per FX session)" `
           -RunLevel Highest -Force

$action2  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument "`"$vbs`" `"$base\run_forex_live_exits.bat`" `"$log`""
$trigger2 = New-ScheduledTaskTrigger -Daily -At "14:00"
$settings2 = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "ATOS Forex LIVE Exit Check" `
            -Action $action2 -Trigger $trigger2 -Settings $settings2 `
            -Description "Real-money Saxo LIVE forex account -- stop/time-stop check only, once daily" `
            -RunLevel Highest -Force

Write-Host ""
Write-Host "Registered 'ATOS Forex LIVE Daily Run' -> 9x/day: $($entryTimes -join ', ') local (entries)."
Write-Host "Registered 'ATOS Forex LIVE Exit Check' -> daily at 14:00 local (exits only)."
Write-Host ""
Write-Host "These will NOT place any real order until SAXO_LIVE_CONFIRMED=1 is set" -ForegroundColor Yellow
Write-Host "as an environment variable visible to Task Scheduler (System or User scope)." -ForegroundColor Yellow
Write-Host "Set it only when you are ready for real orders to start placing automatically:" -ForegroundColor Yellow
Write-Host '  [System.Environment]::SetEnvironmentVariable("SAXO_LIVE_CONFIRMED","1","User")' -ForegroundColor Yellow
Write-Host ""
Write-Host "Remove either task later with:"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Forex LIVE Daily Run' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Forex LIVE Exit Check' -Confirm:`$false"
