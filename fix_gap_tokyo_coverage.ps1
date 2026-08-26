# fix_gap_tokyo_coverage.ps1
# ---------------------------
# Registers "ATOS Forex Gap Tokyo" -- closes a real coverage gap found
# 2026-08-26: the gap strategy's Tokyo session window (00:00-01:30 UTC /
# 05:00-06:30 PKT, Tue-Fri -- Monday's is already covered by the weekly
# window, see forex/runner.py's _detect_gap_session()) had NO dedicated
# task, unlike London/NewYork/Weekly, and fell almost entirely inside the
# regular scan schedule's own dead zone (03:00-06:00 PKT) -- only the
# last ~25 minutes of the 90-minute window got any chance of being
# caught, by the Intraday Scan's first tick at 06:05 PKT.
#
# Fires at 05:00 PKT (00:00 UTC), the exact start of the Tokyo window --
# same pattern as "ATOS Forex Gap London Fixed" (12:00 PKT, start of the
# London window) and "ATOS Forex Gap NewYork" (17:00 PKT, start of the NY
# window): a plain daily trigger invoking the SAME run_forex_daily.bat
# the regular "all strategies" scan uses, relying on the gap strategy's
# own real-time self-gating (_detect_gap_session()) to recognize which
# session is actually active at that moment -- no new .bat file needed.
#
# Verified in code before writing this script: _detect_gap_session()
# correctly resolves to "tokyo" for Tue/Wed/Thu/Fri at 00:00 UTC, and to
# "weekly" for Monday at the same time (the weekly check is evaluated
# first and subsumes Monday's Tokyo window) -- so this task firing daily,
# including Monday, is safe and produces no duplicate/wrong-session work.
#
# RUN THIS ONCE, AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\fix_gap_tokyo_coverage.ps1"

$vbs  = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base = "E:\SaxoTrNew\SaxoTrNew"
$log  = "$base\data\forex_scheduler.log"

$action   = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`" `"$base\run_forex_daily.bat`" `"$log`""
$trigger  = New-ScheduledTaskTrigger -Daily -At "05:00"
$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable `
            -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

try {
    Register-ScheduledTask -TaskName "ATOS Forex Gap Tokyo" `
        -Action $action -Trigger $trigger -Settings $settings `
        -Description "SIM forex -- catches the Tokyo session gap window (00:00-01:30 UTC / 05:00-06:30 PKT, Tue-Fri; Monday's covered by the weekly window). Fires at the start of that window." `
        -RunLevel Highest -Force -ErrorAction Stop | Out-Null
    Write-Host "Registered 'ATOS Forex Gap Tokyo' -> daily at 05:00 PKT (start of the Tokyo gap window)." -ForegroundColor Green
} catch {
    Write-Host "FAILED to register 'ATOS Forex Gap Tokyo': $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Verify with:"
Write-Host "  Get-ScheduledTaskInfo -TaskName 'ATOS Forex Gap Tokyo' | Select-Object LastRunTime,NextRunTime,LastTaskResult"
Write-Host ""
Write-Host "Remove later with:"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Forex Gap Tokyo' -Confirm:`$false"
