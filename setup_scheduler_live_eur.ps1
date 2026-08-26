# setup_scheduler_live_eur.ps1
# ------------------------------
# Registers the Windows Scheduled Tasks for the real-money Saxo LIVE EUR
# sub-account (2026-08-26) -- RSI Pullback only, 83 EXOTIC pairs only,
# 500 EUR code-level cap. Genuinely separate account/state/confirmation-
# flag from the existing SEK LIVE account (setup_scheduler_live.ps1) --
# see forex_live_eur_account_2026-08-26 session notes for the full design
# (including the finding that Saxo pools margin across all 3 sub-accounts,
# so 500 EUR is a code-level sizing cap, not a broker-enforced wall).
#
# Window is 06:00-03:00 PKT (PT21H duration), matching the already-fixed
# SEK account and SIM Intraday Scan window (2026-08-26) -- the FX trading
# day doesn't roll over until ~17:00 New York time (~02:00 PKT during
# EDT), so this covers the tail of the NY session from day one instead of
# needing the same 22:00-cutoff fix applied to it later.
#
# Same CIM-based trigger construction as setup_scheduler_live.ps1 (see
# that file's comments for why New-ScheduledTaskTrigger's -Repetition*
# params alone don't work with -Daily).
#
# RUN THIS ONCE, AS ADMINISTRATOR (re-run any time the schedule changes --
# -Force overwrites the existing task's triggers cleanly):
#   Right-click PowerShell -> Run as Administrator, then:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_live_eur.ps1"
#
# IMPORTANT: these tasks will run "python forex\runner.py --account
# live_eur --strategy rsi --live" but will NOT place any real order until
# SAXO_LIVE_EUR_CONFIRMED=1 is set as a system/user environment variable
# -- a deliberate, separate switch from the SEK account's SAXO_LIVE_
# CONFIRMED (see forex/runner.py's hard rails). Registering these tasks
# does NOT by itself start real trading.

$vbs  = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base = "E:\SaxoTrNew\SaxoTrNew"
$log  = "$base\data\forex_live_eur_scheduler.log"

$action1 = New-ScheduledTaskAction -Execute "wscript.exe" `
           -Argument "`"$vbs`" `"$base\run_forex_live_eur_daily.bat`" `"$log`""
$trigger1 = New-ScheduledTaskTrigger -Daily -At "06:00"
$rep1 = New-CimInstance -CimClass (Get-CimClass -ClassName MSFT_TaskRepetitionPattern -Namespace Root/Microsoft/Windows/TaskScheduler) -ClientOnly
$rep1.Interval = "PT45M"
$rep1.Duration = "PT21H"
$rep1.StopAtDurationEnd = $true
$trigger1.Repetition = $rep1
$settings1 = New-ScheduledTaskSettingsSet `
           -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
           -StartWhenAvailable `
           -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10) `
           -WakeToRun `
           -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

$dailyRunOk = $true
try {
    Register-ScheduledTask -TaskName "ATOS Forex LIVE EUR Daily Run" `
               -Action $action1 -Trigger $trigger1 -Settings $settings1 `
               -Description "Real-money Saxo LIVE EUR sub-account -- RSI Pullback only, 83 exotic pairs, every 45 min 06:00-03:00 PKT" `
               -RunLevel Highest -Force -ErrorAction Stop | Out-Null
} catch {
    $dailyRunOk = $false
    Write-Host "FAILED to register 'ATOS Forex LIVE EUR Daily Run': $($_.Exception.Message)" -ForegroundColor Red
}

$action2  = New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument "`"$vbs`" `"$base\run_forex_live_eur_exits.bat`" `"$log`""
$trigger2 = New-ScheduledTaskTrigger -Daily -At "14:00"
$settings2 = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10) `
            -WakeToRun `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries

$exitCheckOk = $true
try {
    Register-ScheduledTask -TaskName "ATOS Forex LIVE EUR Exit Check" `
                -Action $action2 -Trigger $trigger2 -Settings $settings2 `
                -Description "Real-money Saxo LIVE EUR sub-account -- stop/time-stop check only, once daily" `
                -RunLevel Highest -Force -ErrorAction Stop | Out-Null
} catch {
    $exitCheckOk = $false
    Write-Host "FAILED to register 'ATOS Forex LIVE EUR Exit Check': $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
if ($dailyRunOk) {
    Write-Host "Registered 'ATOS Forex LIVE EUR Daily Run' -> every 45 min, 06:00-03:00 PKT (entries+exits)." -ForegroundColor Green
}
if ($exitCheckOk) {
    Write-Host "Registered 'ATOS Forex LIVE EUR Exit Check' -> daily at 14:00 local (exits only, backstop)." -ForegroundColor Green
}
Write-Host ""
Write-Host "These will NOT place any real order until SAXO_LIVE_EUR_CONFIRMED=1 is set" -ForegroundColor Yellow
Write-Host "as an environment variable visible to Task Scheduler (System or User scope)." -ForegroundColor Yellow
Write-Host "This is SEPARATE from SAXO_LIVE_CONFIRMED (the SEK account's own switch) --" -ForegroundColor Yellow
Write-Host "setting one can never accidentally arm the other." -ForegroundColor Yellow
Write-Host "Set it only when you are ready for real orders to start placing automatically:" -ForegroundColor Yellow
Write-Host '  [System.Environment]::SetEnvironmentVariable("SAXO_LIVE_EUR_CONFIRMED","1","User")' -ForegroundColor Yellow
Write-Host ""
Write-Host "Remove either task later with:"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Forex LIVE EUR Daily Run' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Forex LIVE EUR Exit Check' -Confirm:`$false"
