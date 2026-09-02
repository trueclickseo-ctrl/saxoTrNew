# setup_scheduler_ai_twin.ps1
# ---------------------------
# Registers the 2 Scheduled Tasks for the AI SIM TWIN (2026-09-03).
#
# The twin is a SIM PAPER book (forex/runner.py --account ai_sim +
# atos_ai_stocks.py) where the AI's decision -- the Trading Copilot's
# resize/skip for forex, the basket-ranker's re-ranked pick for stocks --
# is APPLIED. A live forward A/B vs the deterministic SIM books. NO real
# orders anywhere. Compare on `python ai_dashboard.py`.
#
# Requires config/ai.json: enabled_ai_sim + agent_enabled (already true) +
# stocks_ai.enabled -- and ANTHROPIC_API_KEY in the environment (a paid
# Sonnet call per signal). Both tasks are SIM-named so scheduler_watchdog
# treats them as auto-fix-eligible like any other SIM task.
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_ai_twin.ps1"

$vbs  = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base = "E:\SaxoTrNew\SaxoTrNew"
$log  = "$base\data\ai_twin_scheduler.log"

$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 10)

# ── Forex AI twin — every 30 min, 06:00-22:00 PKT (mirrors the SIM Intraday Scan) ──
$a1 = New-ScheduledTaskAction -Execute "wscript.exe" `
      -Argument "`"$vbs`" `"$base\run_forex_ai_scan.bat`" `"$log`""
$t1 = New-ScheduledTaskTrigger -Daily -At "06:00"
$rep = New-CimInstance -CimClass (Get-CimClass -ClassName MSFT_TaskRepetitionPattern -Namespace Root/Microsoft/Windows/TaskScheduler) -ClientOnly
$rep.Interval = "PT30M"; $rep.Duration = "PT16H"; $rep.StopAtDurationEnd = $true
$t1.Repetition = $rep
try {
    Register-ScheduledTask -TaskName "ATOS Forex AI Twin Scan" -Action $a1 -Trigger $t1 `
        -Settings $settings -RunLevel Highest -Force -ErrorAction Stop `
        -Description "AI SIM twin forex scan (--account ai_sim, paper, Copilot resize/skip applied). Every 30 min 06:00-22:00 PKT." | Out-Null
    Write-Host "Registered 'ATOS Forex AI Twin Scan' -> every 30 min 06:00-22:00 PKT." -ForegroundColor Green
} catch { Write-Host "FAILED 'ATOS Forex AI Twin Scan': $($_.Exception.Message)" -ForegroundColor Red }

# ── Stocks AI twin — once daily 02:30 PKT (~30 min after ATOS Daily Run) ──
$a2 = New-ScheduledTaskAction -Execute "wscript.exe" `
      -Argument "`"$vbs`" `"$base\run_atos_ai_stocks.bat`" `"$log`""
$t2 = New-ScheduledTaskTrigger -Daily -At "02:30"
try {
    Register-ScheduledTask -TaskName "ATOS Stocks AI Twin" -Action $a2 -Trigger $t2 `
        -Settings $settings -RunLevel Highest -Force -ErrorAction Stop `
        -Description "AI SIM twin stocks scan (atos_ai_stocks.py, paper, trades the basket-ranker's re-ranked pick). Daily 02:30 PKT." | Out-Null
    Write-Host "Registered 'ATOS Stocks AI Twin' -> daily 02:30 PKT." -ForegroundColor Green
} catch { Write-Host "FAILED 'ATOS Stocks AI Twin': $($_.Exception.Message)" -ForegroundColor Red }

Write-Host ""
Write-Host "Both are SIM paper -- no real orders. Compare: python ai_dashboard.py" -ForegroundColor Yellow
Write-Host "Remove later:"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Forex AI Twin Scan' -Confirm:`$false"
Write-Host "  Unregister-ScheduledTask -TaskName 'ATOS Stocks AI Twin' -Confirm:`$false"
