# setup_scheduler_research_analyst.ps1
# ------------------------------------
# Registers "ATOS AI Research Analyst": runs ai_research_analyst.py --sweep
# ONCE A WEEK (Sunday 04:00 PKT -- quiet, after the week's trades have
# settled). OFFLINE + READ-ONLY: it replays each SIM-roster strategy over
# ~13y of Yahoo daily bars, aggregates the closed-trade record + the AI
# Trading Journal into a digest, has an LLM propose SPECIFIED, testable
# strategy filters, auto-runs the cheap decomposition gate, and appends to
# the triaged backlog (data/ai_research_hypotheses.jsonl). It NEVER edits a
# strategy or touches an order. Gated by config/ai.json
# research_analyst.enabled (ships OFF -- registering the task is harmless
# while the flag is false: the run just prints "OFF" and exits 0).
#
# RUN ONCE, AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_research_analyst.ps1"

$taskName = "ATOS AI Research Analyst"
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument (
    '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ai_research_analyst.bat" "E:\SaxoTrNew\SaxoTrNew\data\ai_research_analyst.log"'
)
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 04:00
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -RunLevel Highest `
        -Description "AI Research Analyst (roadmap #19) -- weekly offline read-only strategy-decomposition + hypothesis backlog. Never trades. config/ai.json research_analyst.enabled gates it." `
        -Force -ErrorAction Stop | Out-Null
    Write-Host "OK   $taskName registered: weekly Sunday 04:00 PKT" -ForegroundColor Green
} catch {
    Write-Host "FAILED ${taskName}: $($_.Exception.Message)" -ForegroundColor Red
}

Write-Host ""
Write-Host "Verify:  Get-ScheduledTaskInfo -TaskName 'ATOS AI Research Analyst' | Select LastRunTime, NextRunTime, LastTaskResult"
Write-Host "Enable:  set config/ai.json research_analyst.enabled = true  (and an ANTHROPIC_API_KEY for the propose step)"
