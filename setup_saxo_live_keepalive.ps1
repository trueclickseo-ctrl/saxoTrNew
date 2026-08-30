# setup_saxo_live_keepalive.ps1
# --------------------------------
# Registers "ATOS Saxo LIVE Token Keepalive": calls
# saxo_auth.get_valid_access_token(env="live") every 10 min, all day, so
# the LIVE refresh-token chain never goes fully cold between the real
# trading runs (spaced ~2h apart, while the refresh_token itself only
# lives 1h -- see saxo_live_token_keepalive.py's module docstring).
#
# This does NOT do the one-time interactive login for you -- if you
# haven't already, run `python saxo_auth.py --live` once yourself first
# (opens a browser, you log into the real Saxo account). After that, this
# task keeps it alive on its own indefinitely.
#
# Hardened 2026-08-30 (the LIVE token kept dying after every reboot / sleep
# gap and needed a manual browser re-login):
#   - runs as SYSTEM  -> fires even when nobody is logged in yet (a reboot
#     that lands before auto-logon used to take the task offline entirely,
#     killing the 1h refresh_token -- a 14.5h outage on 2026-08-29)
#   - StartWhenAvailable -> a run missed while asleep/booting fires on
#     catch-up instead of being silently dropped
#   - 15 min -> 10 min cadence, plus 3x restart-on-failure (1 min apart)
#   - 5 min hard ExecutionTimeLimit so a hung instance can't block the next
#
# RUN THIS ONCE, AS ADMINISTRATOR:
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_saxo_live_keepalive.ps1"

$taskName = "ATOS Saxo LIVE Token Keepalive"
$desc     = "Keeps the real-money Saxo LIVE OAuth session alive every 10 min. Hardened 2026-08-30: SYSTEM principal + StartWhenAvailable + restart-on-failure so a reboot/sleep gap can't kill the 1h refresh_token. See saxo_live_token_keepalive.py."

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument (
    '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_saxo_live_keepalive.bat" "E:\SaxoTrNew\SaxoTrNew\data\saxo_live_keepalive.log"'
)

$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 10) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "WARNING: not running elevated -- the SYSTEM principal will be rejected and this" -ForegroundColor Yellow
    Write-Host "         falls back to '$env:USERNAME (interactive)', which does NOT survive a" -ForegroundColor Yellow
    Write-Host "         reboot-before-logon. For the full fix, re-run from an ADMIN PowerShell." -ForegroundColor Yellow
    Write-Host ""
}

# SYSTEM survives a reboot before any interactive logon; interactive is the
# fallback (still gets StartWhenAvailable + the tighter cadence + restarts).
$sysP  = New-ScheduledTaskPrincipal -UserId "SYSTEM"       -LogonType ServiceAccount -RunLevel Highest
$userP = New-ScheduledTaskPrincipal -UserId $env:USERNAME  -LogonType Interactive    -RunLevel Limited

function Try-Register($principal, $kind) {
    # 1) plain -Force (modify in place)   2) delete + create   -- some boxes block (1)
    try {
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Description $desc -Force -ErrorAction Stop | Out-Null
        return $kind
    } catch {
        Write-Host "  register as $kind (-Force) failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
    try {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Description $desc -ErrorAction Stop | Out-Null
        return "$kind (via delete+create)"
    } catch {
        Write-Host "  register as $kind (delete+create) failed: $($_.Exception.Message)" -ForegroundColor DarkYellow
        return $null
    }
}

$done = Try-Register $sysP "SYSTEM"
if (-not $done) { $done = Try-Register $userP "$env:USERNAME (interactive)" }
if (-not $done) {
    Write-Host "FAILED -- could not register the task at all." -ForegroundColor Red
    exit 1
}
Write-Host "OK   $taskName registered as $done -- every 10 min, StartWhenAvailable, 3x restart-on-failure" -ForegroundColor Green

# Prove it actually works in whatever context it now runs under.
Write-Host ""
Write-Host "Test-running it once..." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 20
$info = Get-ScheduledTaskInfo -TaskName $taskName
Write-Host ("  LastRunTime   : {0}" -f $info.LastRunTime)
Write-Host ("  LastTaskResult: 0x{0:X}  ({1})" -f $info.LastTaskResult, $(if ($info.LastTaskResult -eq 0) { "OK" } else { "NON-ZERO -- check data\saxo_live_keepalive.log" }))
Write-Host ("  NextRunTime   : {0}" -f $info.NextRunTime)
Write-Host ""
Write-Host "Last few keepalive log lines:" -ForegroundColor Cyan
Get-Content "E:\SaxoTrNew\SaxoTrNew\data\saxo_live_keepalive.log" -Tail 4 -ErrorAction SilentlyContinue
Get-Content "E:\SaxoTrNew\SaxoTrNew\data\saxo_live_keepalive.log.fallback" -Tail 4 -ErrorAction SilentlyContinue
