# setup_scheduler_ibkr_blend_v2.ps1
# Registers "ATOS IBKR Blend V2 Rebalance" as a Windows Scheduled Task.
#
# Schedule: Every 14 days (fortnightly) at 19:30 PKT (14:30 UTC / 10:30 ET).
#   Fires 30 min after blend V1 (19:00 PKT) so they don't compete for
#   the same IB Gateway clientId slot. Both fire after US market open.
#
# Executes with --execute --auto: orders place automatically, no prompts.
# Log: data\ibkr_blend_v2_rebalance.log
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler_ibkr_blend_v2.ps1

$ErrorActionPreference = "Stop"

$base     = "E:\SaxoTrNew\SaxoTrNew"
$vbs      = "$base\run_hidden.vbs"
$TaskName = "ATOS IBKR Blend V2 Rebalance"
$LogFile  = "$base\data\ibkr_blend_v2_rebalance.log"

$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_blend_v2.bat" "' + $LogFile + '"')

# Fortnightly on Thursday at 16:30 PKT (same weekday as V1, 30 min later)
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 2 `
    -DaysOfWeek Thursday `
    -At "19:30"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable `
    -WakeToRun

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask `
    -TaskName   $TaskName `
    -Action     $action `
    -Trigger    $trigger `
    -Settings   $settings `
    -RunLevel   Highest `
    -Description "IBKR: US Blend V2 fortnightly rebalance (auto). Skip-month momentum + vol-targeting." `
    -Force | Out-Null

Write-Host "Registered: '$TaskName'"
Write-Host "  Schedule : every 2 weeks (Thursday 19:30 PKT / 10:30 ET)"
Write-Host "  Command  : wscript run_hidden.vbs run_ibkr_blend_v2.bat"
Write-Host "  Log      : $LogFile"
Write-Host ""
Write-Host "To run immediately:"
Write-Host "  schtasks /Run /TN '$TaskName'"
Write-Host ""
Write-Host "To dry-run manually:"
Write-Host "  python run_ibkr_stocks.py --strategy blend_v2"
