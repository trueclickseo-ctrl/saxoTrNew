# setup_saxo_live_keepalive.ps1
# --------------------------------
# Registers "ATOS Saxo LIVE Token Keepalive": calls
# saxo_auth.get_valid_access_token(env="live") every 15 min, all day, so
# the LIVE refresh-token chain never goes fully cold between the real
# trading runs (spaced ~2h apart, while the refresh_token itself only
# lives 1h -- see saxo_live_token_keepalive.py's module docstring).
#
# This does NOT do the one-time interactive login for you -- if you
# haven't already, run `python saxo_auth.py --live` once yourself first
# (opens a browser, you log into the real Saxo account). After that, this
# task keeps it alive on its own indefinitely.
#
# RUN THIS ONCE, AS ADMINISTRATOR:
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_saxo_live_keepalive.ps1"

$taskName = "ATOS Saxo LIVE Token Keepalive"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument (
    '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_saxo_live_keepalive.bat" "E:\SaxoTrNew\SaxoTrNew\data\saxo_live_keepalive.log"'
)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Keeps the real-money Saxo LIVE OAuth session alive every 15 min (see saxo_live_token_keepalive.py)" `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "OK   $taskName registered: every 15 min, indefinitely" -ForegroundColor Green
} catch {
    Write-Host "FAILED ${taskName}: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Verify with:"
Write-Host '  Get-ScheduledTaskInfo -TaskName "ATOS Saxo LIVE Token Keepalive" | Select LastRunTime, NextRunTime, LastTaskResult'
