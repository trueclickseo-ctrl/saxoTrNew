# setup_scheduler_ibkr_blend.ps1
# Registers "ATOS IBKR Blend Rebalance" as a Windows Scheduled Task.
#
# Schedule: Every 14 days (fortnightly) at 16:00 PKT (11:00 UTC).
# The task runs a DRY-RUN blend signal scan — no orders are placed.
# After reviewing the logged signal, run manually with --execute.
#
# Run once as Administrator:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler_ibkr_blend.ps1

$ErrorActionPreference = "Stop"

$TaskName    = "ATOS IBKR Blend Rebalance"
$ScriptRoot  = Split-Path -Parent $MyInvocation.MyCommand.Definition
$PythonExe   = (Get-Command python).Source
$LogFile     = Join-Path $ScriptRoot "data\ibkr_blend_rebalance.log"

# Dry-run only — no --execute flag
$Arguments   = "run_ibkr_stocks.py --strategy blend"

$Action = New-ScheduledTaskAction `
    -Execute    $PythonExe `
    -Argument   $Arguments `
    -WorkingDirectory $ScriptRoot

# Trigger: fortnightly (every 14 days) at 16:00 PKT
# RepetitionInterval not needed for a 14-day cadence; use a weekly trigger
# repeating every 2 weeks.
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -WeeksInterval 2 `
    -DaysOfWeek Thursday `
    -At "16:00"

$Settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Highest

# Unregister any existing task with the same name before re-registering
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $Action `
    -Trigger   $Trigger `
    -Settings  $Settings `
    -Principal $Principal `
    -Force | Out-Null

Write-Host "Registered: '$TaskName'"
Write-Host "  Schedule : every 2 weeks (Thursday 16:00 PKT)"
Write-Host "  Command  : $PythonExe $Arguments"
Write-Host "  Log      : $LogFile"
Write-Host ""
Write-Host "To run immediately (dry-run):"
Write-Host "  python run_ibkr_stocks.py --strategy blend"
Write-Host ""
Write-Host "To place orders after reviewing the signal:"
Write-Host "  python run_ibkr_stocks.py --strategy blend --execute"
