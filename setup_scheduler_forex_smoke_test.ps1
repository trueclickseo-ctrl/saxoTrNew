# Registers a daily pre-flight smoke test that imports forex.runner
# and emails if it fails — before any real forex task runs.
#
# Runs at 05:30 PKT daily (before the 06:00 AI-twin scan).
# If import fails, an email alert fires immediately; watchdog also catches it.
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File setup_scheduler_forex_smoke_test.ps1

$base   = "E:\SaxoTrNew\SaxoTrNew"
$vbs    = "$base\run_hidden.vbs"
$script = "$base\forex_smoke_test.py"
$log    = "$base\data\forex_smoke_test.log"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -WakeToRun

$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument ('"' + $vbs + '" "python ' + $script + '" "' + $log + '"')
$trigger = New-ScheduledTaskTrigger -Daily -At "05:30"

Register-ScheduledTask -TaskName "ATOS Forex Smoke Test" `
    -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Highest -Force | Out-Null

Write-Host "Registered: 'ATOS Forex Smoke Test' (daily 05:30 PKT)"
Write-Host "To test now: schtasks /Run /TN 'ATOS Forex Smoke Test'"
