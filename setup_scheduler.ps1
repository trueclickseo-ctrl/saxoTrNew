# setup_scheduler.ps1
# -------------------
# Registers a Windows Scheduled Task that runs the ATOS daily cycle once a day
# after the US close (your machine is UTC+5, so the US session ends ~01:00-02:00
# local; 07:00 local is safely after all three markets have closed and before
# the European open ~12:00 local). One run covers US + OMX30 + CPH25 because
# ATOS decides on completed daily bars.
#
# RUN THIS ONCE, AS ADMINISTRATOR:
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler.ps1"
#
# IMPORTANT: unattended runs need a VALID Saxo token. The 24h token must be
# refreshed daily until the PKCE auto-refresh flow is enabled (register the
# redirect URI in the Saxo dev portal). Until then, run saxo_auth_auto.py once
# a day (or the scheduled run will fail on the Saxo API calls).

$folder  = "E:\SaxoTrNew\SaxoTrNew"
$runAt   = "07:00"   # local time (UTC+5). Change if you prefer another time after the US close.

$action  = New-ScheduledTaskAction -Execute "py" `
           -Argument "-3 -X utf8 `"$folder\run_atos.py`"" `
           -WorkingDirectory $folder
$trigger = New-ScheduledTaskTrigger -Daily -At $runAt
$settings = New-ScheduledTaskSettingsSet `
           -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
           -StartWhenAvailable `
           -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName "ATOS Daily Run" `
           -Action $action -Trigger $trigger -Settings $settings `
           -Description "ATOS daily algo cycle (US + OMX30 + CPH25) after US close" `
           -RunLevel Highest -Force

Write-Host ""
Write-Host "Registered scheduled task 'ATOS Daily Run' -> daily at $runAt local ($folder)."
Write-Host "Remove it later with:  Unregister-ScheduledTask -TaskName 'ATOS Daily Run' -Confirm:`$false"
Write-Host "Reminder: refresh the Saxo token daily until PKCE auto-refresh is enabled."
