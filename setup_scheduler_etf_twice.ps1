# setup_scheduler_etf_twice.ps1
# --------------------------------
# Reschedules "ATOS ETF Daily Run" to 19:30 PKT (10:30 ET) and registers
# a new "ATOS ETF Midday Run" at 23:00 PKT (14:00 ET).
#
# US market hours: 18:30-01:00 PKT (09:30-16:00 ET). Both runs land inside.
# ETF places Market orders -- must run during exchange hours.
#
# Each run: review_exits -> trail_stops (8% below running high) ->
#           generate_signals -> trim_out_of_ranking (sells positions outside
#           the current top-10) -> process_signals (buys new top-10 entries).
# The trim_out_of_ranking call means any ETF that drops out of the top 10
# ranking is sold on the next run automatically -- no manual intervention.
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_etf_twice.ps1"

$vbs      = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base     = "E:\SaxoTrNew\SaxoTrNew"
$logDaily = "$base\data\etf_scheduler.log"
$logMid   = "$base\data\etf_midday_scheduler.log"

$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -WakeToRun `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

# -- 1. Reschedule existing ETF daily run to 19:30 PKT ------------------------
$action1  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument ('"' + $vbs + '" "' + $base + '\saxo_etf_strategy\run_etf_daily.bat" "' + $logDaily + '"')
$trigger1 = New-ScheduledTaskTrigger -Daily -At "19:30"

try {
    Register-ScheduledTask -TaskName "ATOS ETF Daily Run" `
               -Action $action1 -Trigger $trigger1 -Settings $settings `
               -Description "ETF sector rotation scanner -- US morning session (19:30 PKT / 10:30 ET). Includes trim_out_of_ranking (auto-sells positions outside top 10)." `
               -RunLevel Highest -Force
    Write-Host "OK  Updated:    ATOS ETF Daily Run    -> 19:30 PKT"
} catch {
    $errMsg = $_.Exception.Message
    Write-Host "FAIL $errMsg"
}

# -- 2. Register ETF midday run at 23:00 PKT ----------------------------------
$action2  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument ('"' + $vbs + '" "' + $base + '\saxo_etf_strategy\run_etf_daily.bat" "' + $logMid + '"')
$trigger2 = New-ScheduledTaskTrigger -Daily -At "23:00"

try {
    Register-ScheduledTask -TaskName "ATOS ETF Midday Run" `
               -Action $action2 -Trigger $trigger2 -Settings $settings `
               -Description "ETF sector rotation scanner -- US afternoon session (23:00 PKT / 14:00 ET)." `
               -RunLevel Highest -Force
    Write-Host "OK  Registered: ATOS ETF Midday Run   -> 23:00 PKT"
} catch {
    $errMsg = $_.Exception.Message
    Write-Host "FAIL $errMsg"
}
