# setup_blend_split_tasks.ps1
# Splits the blend rebalance into sells-only (19:30) and buys-only (21:00)
# so reversion entries at 20:35 can use the cash freed by blend sells.
#
# New schedule:
#   19:30 ATOS IBKR Blend Rebalance        <- sells only (updated)
#   19:55 ATOS IBKR Blend Rebalance RETRY  <- sells only retry (updated)
#   20:10 ATOS IBKR Reversion Live Exits   <- unchanged
#   20:25 ATOS IBKR Reversion Live Exits RETRY <- unchanged
#   20:35 ATOS IBKR Reversion Live Entries <- unchanged
#   20:55 ATOS IBKR Reversion Live Entries RETRY <- unchanged
#   21:00 ATOS IBKR Blend Live Buys        <- new: buys only
#   21:25 ATOS IBKR Blend Live Buys RETRY  <- new: buys retry

$root  = "E:\SaxoTrNew\SaxoTrNew"
$vbs   = "$root\run_hidden.vbs"
$user  = "Kwaseem"
$logdir = "$root\logs"

function Make-Action($bat, $log) {
    $args = "`"$vbs`" `"$bat`" `"$logdir\$log`""
    New-ScheduledTaskAction -Execute "wscript.exe" -Argument $args
}

function Make-DailyTrigger($hhmm) {
    $t = New-ScheduledTaskTrigger -Daily -At $hhmm
    return $t
}

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
                                           -StartWhenAvailable

# ── 1. Update "ATOS IBKR Blend Rebalance" -> sells-only bat at 19:30 ─────────
$action  = Make-Action "run_ibkr_blend_live_sells.bat" "ibkr_blend_live_sells.log"
$trigger = Make-DailyTrigger "19:30"
Set-ScheduledTask -TaskName "ATOS IBKR Blend Rebalance" `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings
Write-Host "Updated: ATOS IBKR Blend Rebalance -> sells-only @ 19:30"

# ── 2. Update or create "ATOS IBKR Blend Rebalance RETRY" -> sells-only at 19:55
$action2  = Make-Action "run_ibkr_blend_live_sells.bat" "ibkr_blend_live_sells_retry.log"
$trigger2 = Make-DailyTrigger "19:55"
$existing = Get-ScheduledTask -TaskName "ATOS IBKR Blend Rebalance RETRY" -ErrorAction SilentlyContinue
if ($existing) {
    Set-ScheduledTask -TaskName "ATOS IBKR Blend Rebalance RETRY" `
        -Action $action2 -Trigger $trigger2 -Principal $principal -Settings $settings
    Write-Host "Updated: ATOS IBKR Blend Rebalance RETRY -> sells-only @ 19:55"
} else {
    Register-ScheduledTask -TaskName "ATOS IBKR Blend Rebalance RETRY" `
        -Action $action2 -Trigger $trigger2 -Principal $principal -Settings $settings `
        -Description "Blend sells-only retry 25 min after main run"
    Write-Host "Created: ATOS IBKR Blend Rebalance RETRY @ 19:55"
}

# ── 3. Create "ATOS IBKR Blend Live Buys" at 21:00 ──────────────────────────
$action3  = Make-Action "run_ibkr_blend_live_buys.bat" "ibkr_blend_live_buys.log"
$trigger3 = Make-DailyTrigger "21:00"
$existing3 = Get-ScheduledTask -TaskName "ATOS IBKR Blend Live Buys" -ErrorAction SilentlyContinue
if ($existing3) {
    Set-ScheduledTask -TaskName "ATOS IBKR Blend Live Buys" `
        -Action $action3 -Trigger $trigger3 -Principal $principal -Settings $settings
    Write-Host "Updated: ATOS IBKR Blend Live Buys @ 21:00"
} else {
    Register-ScheduledTask -TaskName "ATOS IBKR Blend Live Buys" `
        -Action $action3 -Trigger $trigger3 -Principal $principal -Settings $settings `
        -Description "Blend buys-only after reversion entries have consumed their cash share"
    Write-Host "Created: ATOS IBKR Blend Live Buys @ 21:00"
}

# ── 4. Create "ATOS IBKR Blend Live Buys RETRY" at 21:25 ────────────────────
$action4  = Make-Action "run_ibkr_blend_live_buys.bat" "ibkr_blend_live_buys_retry.log"
$trigger4 = Make-DailyTrigger "21:25"
$existing4 = Get-ScheduledTask -TaskName "ATOS IBKR Blend Live Buys RETRY" -ErrorAction SilentlyContinue
if ($existing4) {
    Set-ScheduledTask -TaskName "ATOS IBKR Blend Live Buys RETRY" `
        -Action $action4 -Trigger $trigger4 -Principal $principal -Settings $settings
    Write-Host "Updated: ATOS IBKR Blend Live Buys RETRY @ 21:25"
} else {
    Register-ScheduledTask -TaskName "ATOS IBKR Blend Live Buys RETRY" `
        -Action $action4 -Trigger $trigger4 -Principal $principal -Settings $settings `
        -Description "Blend buys-only retry 25 min after main buys run"
    Write-Host "Created: ATOS IBKR Blend Live Buys RETRY @ 21:25"
}

Write-Host ""
Write-Host "Done. New schedule:"
Write-Host "  19:30 Blend SELLS only"
Write-Host "  19:55 Blend SELLS RETRY"
Write-Host "  20:10 Reversion Live Exits"
Write-Host "  20:25 Reversion Live Exits RETRY"
Write-Host "  20:35 Reversion Live Entries  <- uses cash freed by blend sells"
Write-Host "  20:55 Reversion Live Entries RETRY"
Write-Host "  21:00 Blend BUYS only"
Write-Host "  21:25 Blend BUYS RETRY"
