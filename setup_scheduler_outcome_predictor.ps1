# setup_scheduler_outcome_predictor.ps1
# --------------------------------------
# Registers "ATOS AI Outcome Predictor": runs ai_outcome_predictor.py --train
# DAILY at 22:00 PKT (after the US session, before the 23:30 daily email).
# READ-ONLY except for writing data/trade_outcome_model/. Never touches an
# order, position, or stop.
#
# The gate is built into the script: fewer than 100 closed cards from active
# strategies -> prints "not enough data" and exits 0. Once the gate clears it
# trains the GradientBoosting model and saves model.pkl + report.json.
# The model only influences live proposals once you manually flip
# config/ai.json outcome_predictor.enabled: true (after reviewing --report).
#
# RUN ONCE, AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_outcome_predictor.ps1"

$taskName = "ATOS AI Outcome Predictor"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument (
    '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ai_outcome_predictor.bat" "E:\SaxoTrNew\SaxoTrNew\data\ai_outcome_predictor.log"'
)
$trigger = New-ScheduledTaskTrigger -Daily -At 22:00
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Highest `
        -Description "Trade Outcome Predictor (roadmap #20) -- daily auto-train once 100 closed cards from active strategies. Gate built-in: safe to run daily before gate clears. Never trades." `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "OK   $taskName registered: daily 22:00 PKT" -ForegroundColor Green
} catch {
    Write-Host "FAILED ${taskName}: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Verify:  Get-ScheduledTaskInfo -TaskName 'ATOS AI Outcome Predictor' | Select LastRunTime, NextRunTime, LastTaskResult"
Write-Host "Log:     data\ai_outcome_predictor.log"
Write-Host "After gate clears: check data\ai_outcome_predictor.log, then:"
Write-Host "  python ai_outcome_predictor.py --report"
Write-Host "  If lift >= +5%: set config/ai.json outcome_predictor.enabled = true"
