# Registers two Windows Task Scheduler tasks for the 4 US Signals strategies on IBKR.
#
#   "ATOS IBKR Signals Entries" -- daily at 16:00 PKT (07:00 ET, before US open)
#   "ATOS IBKR Signals Exits"   -- daily at 09:00 PKT (00:00 ET, after US close)
#
# Both are DRY-RUN only (scan + log; no orders placed automatically).
# Execution is always manual: run_ibkr_signals.bat --execute
#
# Strategies: US SMA Crossover, US RSI Reversal, US Momentum, US Ensemble
# Budget: $2,000/slot, max 5 slots per strategy (20 slots across 4 strategies)
# Database: data/ibkr_stocks.db (shared with blend/reversion, keyed by strategy column)
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler_ibkr_signals.ps1

$base    = "E:\SaxoTrNew\SaxoTrNew"
$vbs     = "$base\run_hidden.vbs"
$python  = "$base\.venv\Scripts\python.exe"

$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
            -StartWhenAvailable -WakeToRun `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

# ── Entries task ──────────────────────────────────────────────────────────────

$logEntries = "$base\data\ibkr_signals_entries.log"
$actionEntries = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_signals.bat" "' + $logEntries + '"')
$triggerEntries = New-ScheduledTaskTrigger -Daily -At "16:00"

try {
    Register-ScheduledTask -TaskName "ATOS IBKR Signals Entries" `
        -Action $actionEntries -Trigger $triggerEntries -Settings $settings `
        -RunLevel Highest -Force | Out-Null
    Write-Host "Registered: 'ATOS IBKR Signals Entries'  (daily 16:00 PKT)"
} catch {
    Write-Warning "Failed to register 'ATOS IBKR Signals Entries': $_"
}

# ── Exits task ────────────────────────────────────────────────────────────────

$logExits = "$base\data\ibkr_signals_exits.log"
$actionExits = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_signals.bat --exits" "' + $logExits + '"')
$triggerExits = New-ScheduledTaskTrigger -Daily -At "09:00"

try {
    Register-ScheduledTask -TaskName "ATOS IBKR Signals Exits" `
        -Action $actionExits -Trigger $triggerExits -Settings $settings `
        -RunLevel Highest -Force | Out-Null
    Write-Host "Registered: 'ATOS IBKR Signals Exits'    (daily 09:00 PKT)"
} catch {
    Write-Warning "Failed to register 'ATOS IBKR Signals Exits': $_"
}

Write-Host ""
Write-Host "Done. Watchdog monitors: ibkr_signals_entries.log / ibkr_signals_exits.log"
Write-Host "To test now:  schtasks /Run /TN 'ATOS IBKR Signals Entries'"
