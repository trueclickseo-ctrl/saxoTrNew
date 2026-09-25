# setup_scheduler_ibkr_weekly_report.ps1
# Registers "IBKR Live Weekly Report" as a Windows Scheduled Task.
#
# Schedule: Every Friday at 21:00 PKT (16:00 UTC / 11:00 ET).
# Sends a rich HTML email to atoslive500@gmail.com with:
#   - Account equity breakdown (SVG bar chart)
#   - Strategy performance (Blend vs Reversion)
#   - Per-ticker realized P&L chart
#   - Open positions with unrealized P&L (Yahoo Finance prices)
#   - Closed trades table (this week + all-time)
# Log: data\ibkr_weekly_report.log
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler_ibkr_weekly_report.ps1

$ErrorActionPreference = "Stop"

$base     = "E:\SaxoTrNew\SaxoTrNew"
$vbs      = "$base\run_hidden.vbs"
$TaskName = "IBKR Live Weekly Report"
$LogFile  = "$base\data\ibkr_weekly_report.log"

$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_weekly_report.bat" "' + $LogFile + '"')

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Friday `
    -At "21:00"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15) `
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
    -Description "IBKR LIVE: weekly portfolio report email — equity, strategy stats, open positions, closed trades." `
    -Force | Out-Null

Write-Host "Registered: '$TaskName'"
Write-Host "  Schedule : every Friday at 21:00 PKT (16:00 UTC)"
Write-Host "  Command  : wscript run_hidden.vbs run_ibkr_weekly_report.bat"
Write-Host "  Log      : $LogFile"
Write-Host "  Email to : atoslive500@gmail.com (LIVE routing)"
Write-Host ""
Write-Host "To run immediately:"
Write-Host "  schtasks /Run /TN '$TaskName'"
Write-Host ""
Write-Host "To test manually:"
Write-Host "  python ibkr_live_weekly_report.py"
