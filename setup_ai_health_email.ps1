# setup_ai_health_email.ps1
# --------------------------
# Registers "ATOS AI Health Email": sends ai_shadow_health.py --email
# TWICE a day (09:00 and 21:00 PKT) -- a positive "the AI shadow study is
# up and green" heartbeat (or the problems, if any). The scheduler
# watchdog still sends problem-only alerts separately; this is the
# confirmation-it's-alive signal that was missing.
#
# RUN ONCE, AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_ai_health_email.ps1"

$taskName = "ATOS AI Health Email"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument (
    '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ai_health_email.bat" "E:\SaxoTrNew\SaxoTrNew\data\ai_health_email.log"'
)
$t1 = New-ScheduledTaskTrigger -Daily -At 09:00
$t2 = New-ScheduledTaskTrigger -Daily -At 21:00
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($t1, $t2) `
        -Settings $settings -Description "AI shadow-study heartbeat email, 09:00 + 21:00 PKT (ai_shadow_health.py --email)" `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "OK   $taskName registered: daily 09:00 and 21:00" -ForegroundColor Green
} catch {
    Write-Host "FAILED ${taskName}: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Verify:  Get-ScheduledTaskInfo -TaskName 'ATOS AI Health Email' | Select LastRunTime, NextRunTime, LastTaskResult"
