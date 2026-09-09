# setup_scheduler_ibkr_reversion_v2.ps1
# ---------------------------------------
# Registers two IBKR Reversion V2 tasks in Windows Task Scheduler.
#
#  "ATOS IBKR Reversion V2 Entries" -- 16:00 PKT daily (07:00 ET, before US open)
#    Dry-run scan with enhanced filters: SPY regime, EMA200*1.02, R:R gate,
#    volume-weighted score. Review ibkr_reversion_v2_entries.log, then --execute.
#
#  "ATOS IBKR Reversion V2 Exits" -- 09:00 PKT daily (00:00 ET, after US close)
#    Dry-run exit check for open reversion_v2 positions. Review log, then --execute.
#
# V2 runs alongside V1 as an A/B twin — strategy tag "reversion_v2", client_id 18.
# Both tasks are dry-run only: no orders placed automatically.
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_ibkr_reversion_v2.ps1"

$vbs  = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base = "E:\SaxoTrNew\SaxoTrNew"

$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -WakeToRun `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

# -- Task 1: Reversion V2 Entries ---------------------------------------------
$entriesLog = "$base\data\ibkr_reversion_v2_entries.log"
$action1    = New-ScheduledTaskAction -Execute "wscript.exe" `
              -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_reversion_v2_entries.bat" "' + $entriesLog + '"')
$trigger1   = New-ScheduledTaskTrigger -Daily -At "16:00"

try {
    Register-ScheduledTask -TaskName "ATOS IBKR Reversion V2 Entries" `
               -Action $action1 -Trigger $trigger1 -Settings $settings `
               -Description "IBKR: dry-run reversion V2 entry scan (SPY regime + R:R gate). 16:00 PKT / 07:00 ET." `
               -RunLevel Highest -Force -ErrorAction Stop | Out-Null
    Write-Host "OK  Registered: ATOS IBKR Reversion V2 Entries -> 16:00 PKT"
} catch {
    Write-Host "FAIL  ATOS IBKR Reversion V2 Entries: $($_.Exception.Message)"
}

# -- Task 2: Reversion V2 Exits -----------------------------------------------
$exitsLog = "$base\data\ibkr_reversion_v2_exits.log"
$action2  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_reversion_v2_exits.bat" "' + $exitsLog + '"')
$trigger2 = New-ScheduledTaskTrigger -Daily -At "09:00"

try {
    Register-ScheduledTask -TaskName "ATOS IBKR Reversion V2 Exits" `
               -Action $action2 -Trigger $trigger2 -Settings $settings `
               -Description "IBKR: dry-run reversion V2 exit check. 09:00 PKT / 00:00 ET (after US close)." `
               -RunLevel Highest -Force -ErrorAction Stop | Out-Null
    Write-Host "OK  Registered: ATOS IBKR Reversion V2 Exits -> 09:00 PKT"
} catch {
    Write-Host "FAIL  ATOS IBKR Reversion V2 Exits: $($_.Exception.Message)"
}
