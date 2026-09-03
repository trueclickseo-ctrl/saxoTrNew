# setup_scheduler_stock_outcome_predictor.ps1
# --------------------------------------------
# Registers "ATOS Stock Outcome Predictor": runs ai_stock_outcome_predictor.py --train
# DAILY at 22:10 PKT (10 min after the forex TOP, same daily session window).
# READ-ONLY except for writing data/stock_outcome_model/.  Never trades.
#
# Gate built-in: fewer than 50 closed stock trades -> "not enough data", exit 0.
# enabled: true is a HUMAN step after reviewing --report (lift >= +5% threshold).
#
# RUN ONCE, AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_stock_outcome_predictor.ps1"

$taskName = "ATOS Stock Outcome Predictor"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument (
    '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ai_stock_outcome_predictor.bat" "E:\SaxoTrNew\SaxoTrNew\data\ai_stock_outcome_predictor.log"'
)
$trigger = New-ScheduledTaskTrigger -Daily -At 22:10
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Highest `
        -Description "Stock Trade Outcome Predictor (roadmap #20 sibling) -- daily auto-train once 50 closed stock trades. Gate built-in: safe to run daily before gate clears. Never trades." `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "OK   $taskName registered: daily 22:10 PKT" -ForegroundColor Green
} catch {
    Write-Host "FAILED ${taskName}: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Verify:  Get-ScheduledTaskInfo -TaskName 'ATOS Stock Outcome Predictor' | Select LastRunTime, NextRunTime, LastTaskResult"
Write-Host "Log:     data\ai_stock_outcome_predictor.log"
Write-Host "After gate clears: check log, then:"
Write-Host "  python ai_stock_outcome_predictor.py --report"
Write-Host "  If lift >= +5%: set config/ai.json stock_outcome_predictor.enabled = true"
