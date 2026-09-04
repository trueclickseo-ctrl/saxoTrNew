# setup_scheduler_ibkr_reversion.ps1
# ------------------------------------
# Registers two IBKR Reversion tasks in Windows Task Scheduler.
#
#  "ATOS IBKR Reversion Entries" -- 16:00 PKT daily (07:00 ET, before US open)
#    Dry-run scan for RSI < 38 dip setups using yesterday's Yahoo closes.
#    Review ibkr_reversion_entries.log, then execute manually with --execute.
#
#  "ATOS IBKR Reversion Exits" -- 09:00 PKT daily (00:00 ET, after US close)
#    Dry-run exit check for open reversion positions (RSI recovery / SMA target
#    / time-stop). Review ibkr_reversion_exits.log, then execute with --exits --execute.
#
# Both tasks require IB Gateway running and the paper account funded.
# Protective dry-run only: no orders placed automatically.
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_ibkr_reversion.ps1"

$vbs  = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base = "E:\SaxoTrNew\SaxoTrNew"

$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -WakeToRun `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

# ── Task 1: Reversion Entries ─────────────────────────────────────────────────
$entriesLog = "$base\data\ibkr_reversion_entries.log"
$action1    = New-ScheduledTaskAction -Execute "wscript.exe" `
              -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_reversion_entries.bat" "' + $entriesLog + '"')
$trigger1   = New-ScheduledTaskTrigger -Daily -At "16:00"

try {
    Register-ScheduledTask -TaskName "ATOS IBKR Reversion Entries" `
               -Action $action1 -Trigger $trigger1 -Settings $settings `
               -Description "IBKR: dry-run reversion entry scan (RSI<38 dip). 16:00 PKT / 07:00 ET." `
               -RunLevel Highest -Force
    Write-Host "OK  Registered: ATOS IBKR Reversion Entries -> 16:00 PKT"
} catch {
    Write-Host "FAIL $($_.Exception.Message)"
}

# ── Task 2: Reversion Exits ───────────────────────────────────────────────────
$exitsLog = "$base\data\ibkr_reversion_exits.log"
$action2  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_reversion_exits.bat" "' + $exitsLog + '"')
$trigger2 = New-ScheduledTaskTrigger -Daily -At "09:00"

try {
    Register-ScheduledTask -TaskName "ATOS IBKR Reversion Exits" `
               -Action $action2 -Trigger $trigger2 -Settings $settings `
               -Description "IBKR: dry-run reversion exit check. 09:00 PKT / 00:00 ET (after US close)." `
               -RunLevel Highest -Force
    Write-Host "OK  Registered: ATOS IBKR Reversion Exits -> 09:00 PKT"
} catch {
    Write-Host "FAIL $($_.Exception.Message)"
}
