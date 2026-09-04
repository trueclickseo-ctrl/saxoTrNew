#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Reschedule IBKR tasks to US market hours + add missing Scorer task.
    Run this script as Administrator (right-click -> Run with PowerShell as Admin).

    NEW SCHEDULE (all PKT = UTC+5):
      Scorer Entries       Mon-Fri 19:00 PKT  (10:00 ET, 30 min after open)
      Reversion Entries    Mon-Fri 19:30 PKT  (10:30 ET)
      Signals Entries      Mon-Fri 19:30 PKT  (10:30 ET)
      Blend Rebalance      Thu     19:30 PKT  (10:30 ET)
      Signals Exits        Mon-Fri 22:00 PKT  (13:00 ET, mid-session)
      Reversion Exits      Mon-Fri 23:00 PKT  (14:00 ET)
      Intraday Reversion   Already correct (19:00/20:30/22:00/23:30/00:30 PKT)
#>

$ROOT    = "E:\SaxoTrNew\SaxoTrNew"
$BACKUP  = "$ROOT\data\task_backup"
$PYTHON  = "C:\Program Files\Python311\python.exe"
$VBS     = "$ROOT\run_hidden.vbs"

Set-Location $ROOT

function Recreate-Task {
    param($TaskName, $XmlFile)
    Write-Host "`n=== $TaskName ==="
    schtasks /Delete /TN $TaskName /F 2>&1 | Write-Host
    schtasks /Create /TN $TaskName /XML $XmlFile /F 2>&1 | Write-Host
}

# ── Reschedule 5 existing tasks from pre-built fixed XMLs ─────────────────────
Recreate-Task "ATOS IBKR Blend Rebalance"   "$BACKUP\ATOS_IBKR_Blend_Rebalance_fixed.xml"
Recreate-Task "ATOS IBKR Reversion Entries" "$BACKUP\ATOS_IBKR_Reversion_Entries_fixed.xml"
Recreate-Task "ATOS IBKR Reversion Exits"   "$BACKUP\ATOS_IBKR_Reversion_Exits_fixed.xml"
Recreate-Task "ATOS IBKR Signals Entries"   "$BACKUP\ATOS_IBKR_Signals_Entries_fixed.xml"
Recreate-Task "ATOS IBKR Signals Exits"     "$BACKUP\ATOS_IBKR_Signals_Exits_fixed.xml"

# ── Create new IBKR Scorer task (Mon-Fri 19:00 PKT) ─────────────────────────
Write-Host "`n=== ATOS IBKR Scorer (NEW) ==="
$scorerBat = "$ROOT\run_ibkr_scorer.bat"
$scorerLog = "$ROOT\data\ibkr_scorer.log"

schtasks /Delete /TN "ATOS IBKR Scorer" /F 2>&1 | Out-Null

schtasks /Create `
  /TN "ATOS IBKR Scorer" `
  /TR "wscript.exe `"$VBS`" `"$scorerBat`" `"$scorerLog`"" `
  /SC WEEKLY `
  /D MON,TUE,WED,THU,FRI `
  /ST 19:00 `
  /RL HIGHEST `
  /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK: ATOS IBKR Scorer created -> Mon-Fri 19:00 PKT"
} else {
    Write-Host "FAIL: could not create scorer task"
}

# ── Create IBKR Trail Stops task (Mon-Fri 21:00 PKT = exit check equivalent) ─
Write-Host "`n=== ATOS IBKR Trail Stops (NEW) ==="
$trailBat = "$ROOT\run_ibkr_trail_stops.bat"
$trailLog = "$ROOT\data\ibkr_trail_stops.log"

schtasks /Delete /TN "ATOS IBKR Trail Stops" /F 2>&1 | Out-Null

schtasks /Create `
  /TN "ATOS IBKR Trail Stops" `
  /TR "wscript.exe `"$VBS`" `"$trailBat`" `"$trailLog`"" `
  /SC WEEKLY `
  /D MON,TUE,WED,THU,FRI `
  /ST 21:00 `
  /RL HIGHEST `
  /F
if ($LASTEXITCODE -eq 0) {
    Write-Host "OK: ATOS IBKR Trail Stops created -> Mon-Fri 21:00 PKT"
} else {
    Write-Host "FAIL: could not create trail stops task"
}

# ── Verify final schedule ─────────────────────────────────────────────────────
Write-Host "`n`n=== FINAL IBKR SCHEDULE ==="
$ibkrTasks = @(
    "ATOS IBKR Scorer",
    "ATOS IBKR Blend Rebalance",
    "ATOS IBKR Reversion Entries",
    "ATOS IBKR Reversion Exits",
    "ATOS IBKR Signals Entries",
    "ATOS IBKR Signals Exits",
    "ATOS IBKR Intraday Reversion"
)
foreach ($t in $ibkrTasks) {
    $info = schtasks /query /fo LIST /v /tn $t 2>$null | Select-String "Next Run Time"
    Write-Host ("  " + $t.PadRight(35) + " -> " + ($info -join " ").Trim())
}
Write-Host "`nDone."
