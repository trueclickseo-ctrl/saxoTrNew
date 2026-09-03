# setup_scheduler_avanza_trail_stops.ps1
# ----------------------------------------
# Registers "ATOS Avanza Trail Stops" in Windows Task Scheduler.
# Runs daily at 21:00 PKT (12:00 ET, midday US session) to ratchet
# Avanza stop-loss orders upward as positions appreciate.
#
# Protective-only: no new positions opened. Credentials come from
# .env.avanza (loaded inside run_avanza.py). Only ratchets stops UP.
#
# Watchdog key: "Avanza Trail Stops" -> max_log_age_hours=26.
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_avanza_trail_stops.ps1"

$vbs     = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base    = "E:\SaxoTrNew\SaxoTrNew"
$logFile = "$base\data\avanza_trail_stops.log"

$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -WakeToRun `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

$action  = New-ScheduledTaskAction -Execute "wscript.exe" `
           -Argument ('"' + $vbs + '" "' + $base + '\run_avanza_trail_stops.bat" "' + $logFile + '"')
$trigger = New-ScheduledTaskTrigger -Daily -At "21:00"

try {
    Register-ScheduledTask -TaskName "ATOS Avanza Trail Stops" `
               -Action $action -Trigger $trigger -Settings $settings `
               -Description "Avanza ISK: ratchet stop-loss orders up as positions appreciate. 21:00 PKT / 12:00 ET." `
               -RunLevel Highest -Force
    Write-Host "OK  Registered: ATOS Avanza Trail Stops -> 21:00 PKT"
} catch {
    $errMsg = $_.Exception.Message
    Write-Host "FAIL $errMsg"
}
