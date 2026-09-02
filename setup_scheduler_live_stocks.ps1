# setup_scheduler_live_stocks.ps1
# -------------------------------
# Registers the Windows Scheduled Tasks for the real-money ATOS LIVE STOCKS
# sleeve (US Blend, SEK LIVE sub-account, 30k SEK). 2026-09-02.
#
# SEPARATE module from ATOS LIVE FOREX -- its own tasks, own log
# (data/stocks_live_scheduler.log), own .bat wrappers. Shares only the Saxo
# SEK sub-account login.
#
# LIVE since 2026-09-03 (explicit user instruction). Both .bat wrappers pass
# --live; real orders also require the User env vars below (set 2026-09-03):
#   [System.Environment]::SetEnvironmentVariable("SAXO_LIVE_STOCKS_CONFIRMED","1","User")
#   [System.Environment]::SetEnvironmentVariable("LIVE_STOCKS_DRY_RUN","0","User")
# To go back to observe-only WITHOUT touching the .bat: setx LIVE_STOCKS_DRY_RUN 1
# (or remove SAXO_LIVE_STOCKS_CONFIRMED), then reboot / new logon.
#
# Cadence: US Blend is a 14-day rebalance + a daily risk-off/event/stop
# overlay -- no "still-forming candle" logic needed. BUT the run must land
# INSIDE US regular hours: LIVE places real Market stock orders and Saxo
# rejects those when the exchange is closed (no paper-fill on LIVE, unlike
# SIM). US RTH = 09:30-16:00 ET = 18:30-01:00 PKT.
#   Daily Run  19:20 PKT  (~50 min after the open -- opening prints settled,
#              full session left for a market order to fill / retry). Still
#              decides off yesterday's daily close (the momentum reference).
#   Exit Check 23:30 PKT  (~mid-session, 13:00 ET) -- risk-off / corp-event /
#              stop management on the open book, no new buys.
# (Was 02:40 / 14:00 PKT during Phase-1 observe, when fills didn't matter.)
#
# RUN THIS ONCE, AS ADMINISTRATOR (re-run any time the schedule changes):
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_live_stocks.ps1"

$vbs  = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base = "E:\SaxoTrNew\SaxoTrNew"
$log  = "$base\data\stocks_live_scheduler.log"

$settings = New-ScheduledTaskSettingsSet `
           -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
           -StartWhenAvailable -WakeToRun `
           -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

# ── Daily Run (rebalance + overlay) ─────────────────────────────────────
$action1 = New-ScheduledTaskAction -Execute "wscript.exe" `
           -Argument "`"$vbs`" `"$base\run_atos_live_stocks_daily.bat`" `"$log`""
$trigger1 = New-ScheduledTaskTrigger -Daily -At "19:20"

$dailyOk = $true
try {
    Register-ScheduledTask -TaskName "ATOS Stocks LIVE Daily Run" `
               -Action $action1 -Trigger $trigger1 -Settings $settings `
               -Description "Real-money US Blend stocks sleeve (30k SEK, SEK LIVE sub-account). LIVE since 2026-09-03 -- .bat passes --live; env vars are the real gate." `
               -RunLevel Highest -Force -ErrorAction Stop | Out-Null
} catch {
    $dailyOk = $false
    Write-Host "FAILED to register 'ATOS Stocks LIVE Daily Run': $($_.Exception.Message)" -ForegroundColor Red
}

# ── Exit Check backstop ────────────────────────────────────────────────
$action2 = New-ScheduledTaskAction -Execute "wscript.exe" `
           -Argument "`"$vbs`" `"$base\run_atos_live_stocks_exits.bat`" `"$log`""
$trigger2 = New-ScheduledTaskTrigger -Daily -At "23:30"

$exitOk = $true
try {
    Register-ScheduledTask -TaskName "ATOS Stocks LIVE Exit Check" `
               -Action $action2 -Trigger $trigger2 -Settings $settings `
               -Description "Real-money US Blend stocks sleeve -- stop/risk-off/event exit check only, once daily. LIVE since 2026-09-03." `
               -RunLevel Highest -Force -ErrorAction Stop | Out-Null
} catch {
    $exitOk = $false
    Write-Host "FAILED to register 'ATOS Stocks LIVE Exit Check': $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
if ($dailyOk) { Write-Host "Registered 'ATOS Stocks LIVE Daily Run'  -> daily 19:20 PKT (in US hours -- rebalance + overlay)." -ForegroundColor Green }
if ($exitOk)  { Write-Host "Registered 'ATOS Stocks LIVE Exit Check' -> daily 23:30 PKT (mid US session -- exits only)." -ForegroundColor Green }
Write-Host ""
Write-Host "LIVE: .bat passes --live. Real orders require SAXO_LIVE_STOCKS_CONFIRMED=1 + LIVE_STOCKS_DRY_RUN=0 (User env)." -ForegroundColor Yellow
Write-Host ""
Write-Host "Remove later with:"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Stocks LIVE Daily Run'  -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Stocks LIVE Exit Check' -Confirm:`$false"
