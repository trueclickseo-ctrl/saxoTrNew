# setup_scheduler_futures_midday.ps1
# ------------------------------------
# Reschedules the existing "ATOS Futures Daily Run" to 19:30 PKT (10:30 ET,
# mid US morning session) and registers a new "ATOS Futures Midday Run" at
# 23:00 PKT (14:00 ET, mid US afternoon session).
#
# US market hours: 18:30-01:00 PKT (09:30-16:00 ET). Both runs land inside.
# The 8% trailing stop now also fetches a live Saxo quote when chart data is
# unavailable (ContractFutures SIM restriction), so intraday runs are useful
# even on instruments where daily bars are not refreshed until the close.
#
# Max gap between runs: 23:00 -> next day 19:30 = 20.5h.
# Watchdog max_log_age_hours updated to 22 to cover this gap.
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_futures_midday.ps1"

$vbs      = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base     = "E:\SaxoTrNew\SaxoTrNew"
$logDaily = "$base\data\futures_scheduler.log"
$logMid   = "$base\data\futures_midday_scheduler.log"

$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -WakeToRun `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

# -- 1. Reschedule existing daily run to 19:30 PKT ----------------------------
$action1  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument ('"' + $vbs + '" "' + $base + '\run_futures_daily.bat" "' + $logDaily + '"')
$trigger1 = New-ScheduledTaskTrigger -Daily -At "19:30"

try {
    Register-ScheduledTask -TaskName "ATOS Futures Daily Run" `
               -Action $action1 -Trigger $trigger1 -Settings $settings `
               -Description "Futures Donchian scanner -- US morning session (19:30 PKT / 10:30 ET)." `
               -RunLevel Highest -Force
    Write-Host "OK  Updated:    ATOS Futures Daily Run  -> 19:30 PKT"
} catch {
    $errMsg = $_.Exception.Message
    Write-Host "FAIL $errMsg"
}

# -- 2. Register midday run at 23:00 PKT --------------------------------------
$action2  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument ('"' + $vbs + '" "' + $base + '\run_futures_daily.bat" "' + $logMid + '"')
$trigger2 = New-ScheduledTaskTrigger -Daily -At "23:00"

try {
    Register-ScheduledTask -TaskName "ATOS Futures Midday Run" `
               -Action $action2 -Trigger $trigger2 -Settings $settings `
               -Description "Futures Donchian scanner -- US afternoon session (23:00 PKT / 14:00 ET)." `
               -RunLevel Highest -Force
    Write-Host "OK  Registered: ATOS Futures Midday Run -> 23:00 PKT"
} catch {
    $errMsg = $_.Exception.Message
    Write-Host "FAIL $errMsg"
}
