# setup_scheduler_ibkr_blend.ps1
# Registers "ATOS IBKR Blend Rebalance" as a Windows Scheduled Task.
#
# Schedule: Every 14 days (fortnightly) at 19:00 PKT (14:00 UTC / 10:00 ET).
# Fires 30 min after US market open (18:30 PKT) so IBKR prices are stable.
# Runs automatically: --execute --auto (orders placed without prompts).
# Log: data\ibkr_blend_rebalance.log
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler_ibkr_blend.ps1

$ErrorActionPreference = "Stop"

$base     = "E:\SaxoTrNew\SaxoTrNew"
$vbs      = "$base\run_hidden.vbs"
$TaskName = "ATOS IBKR Blend Rebalance"
$LogFile  = "$base\data\ibkr_blend_rebalance.log"

$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_blend.bat" "' + $LogFile + '"')

# Fortnightly on Thursday at 16:00 PKT
$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 2 `
    -DaysOfWeek Thursday `
    -At "19:00"

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
    -Description "IBKR: US Blend fortnightly rebalance (auto). Cross-sectional momentum, 14-day cadence." `
    -Force | Out-Null

Write-Host "Registered: '$TaskName'"
Write-Host "  Schedule : every 2 weeks (Thursday 19:00 PKT / 10:00 ET)"
Write-Host "  Command  : wscript run_hidden.vbs run_ibkr_blend.bat"
Write-Host "  Log      : $LogFile"
Write-Host ""
Write-Host "To run immediately:"
Write-Host "  schtasks /Run /TN '$TaskName'"
Write-Host ""
Write-Host "To dry-run manually:"
Write-Host "  python run_ibkr_stocks.py --strategy blend"
