# setup_saxo_sim_keepalive.ps1
# -----------------------------
# Registers "ATOS Saxo SIM Token Keepalive": calls
# saxo_auth.get_valid_access_token(env="sim") every 15 min, all day, so
# the SIM refresh-token chain never goes fully cold across the overnight
# scan gap (the ATOS Forex Intraday Scan runs 06:05 -> ~03:00 PKT then
# ~3h with nothing, while a PKCE SIM refresh_token only lives ~60 min --
# see saxo_sim_token_keepalive.py's module docstring).
#
# This does NOT do the one-time interactive login for you -- run
# `python saxo_auth.py` once yourself first (opens a browser, you log
# into the Saxo SIM account). After that, this task keeps it alive on its
# own indefinitely.
#
# RUN THIS ONCE, AS ADMINISTRATOR:
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_saxo_sim_keepalive.ps1"

$taskName = "ATOS Saxo SIM Token Keepalive"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument (
    '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_saxo_sim_keepalive.bat" "E:\SaxoTrNew\SaxoTrNew\data\saxo_sim_keepalive.log"'
)
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Keeps the Saxo SIM OAuth session alive every 15 min (see saxo_sim_token_keepalive.py)" `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "OK   $taskName registered: every 15 min, indefinitely" -ForegroundColor Green
} catch {
    Write-Host "FAILED ${taskName}: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Verify with:"
Write-Host '  Get-ScheduledTaskInfo -TaskName "ATOS Saxo SIM Token Keepalive" | Select LastRunTime, NextRunTime, LastTaskResult'
