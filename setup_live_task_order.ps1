# Reorders IBKR Live scheduled tasks so blend sells first (generates cash),
# then reversion entries run after cash is available.
#
# New schedule:
#   19:30 - Blend Rebalance        (sell US blend + buy blend)
#   19:55 - Blend Rebalance RETRY
#   20:10 - Reversion Live Exits   (close bounced reversion positions)
#   20:25 - Reversion Live Exits RETRY
#   20:35 - Reversion Live Entries (buy reversion with cash from blend sells)
#   20:55 - Reversion Live Entries RETRY
#   21:00 - Trail Stops Live       (unchanged)

$principal = New-ScheduledTaskPrincipal -UserId "Kwaseem" -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable

$tasks = @(
    @{ Name = "ATOS IBKR Blend Rebalance";               Arg = '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ibkr_blend_live.bat" "E:\SaxoTrNew\SaxoTrNew\data\ibkr_blend_live_rebalance.log"';               Time = "19:30" },
    @{ Name = "ATOS IBKR Blend Rebalance RETRY";          Arg = '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ibkr_blend_live.bat" "E:\SaxoTrNew\SaxoTrNew\data\ibkr_blend_live_rebalance_retry.log"';         Time = "19:55" },
    @{ Name = "ATOS IBKR Reversion Live Exits";           Arg = '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ibkr_reversion_live_exits.bat" "E:\SaxoTrNew\SaxoTrNew\data\ibkr_reversion_live_exits.log"';       Time = "20:10" },
    @{ Name = "ATOS IBKR Reversion Live Exits RETRY";     Arg = '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ibkr_reversion_live_exits.bat" "E:\SaxoTrNew\SaxoTrNew\data\ibkr_reversion_live_exits_retry.log"'; Time = "20:25" },
    @{ Name = "ATOS IBKR Reversion Live Entries";         Arg = '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ibkr_reversion_live.bat" "E:\SaxoTrNew\SaxoTrNew\data\ibkr_reversion_live.log"';                  Time = "20:35" },
    @{ Name = "ATOS IBKR Reversion Live Entries RETRY";   Arg = '"E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs" "E:\SaxoTrNew\SaxoTrNew\run_ibkr_reversion_live.bat" "E:\SaxoTrNew\SaxoTrNew\data\ibkr_reversion_live_retry.log"';            Time = "20:55" }
)

foreach ($t in $tasks) {
    $action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument $t.Arg
    $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
    try {
        Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force -ErrorAction Stop
        Write-Host "OK  $($t.Time)  $($t.Name)"
    } catch {
        Write-Host "ERR $($t.Time)  $($t.Name) -- $_"
    }
}
Write-Host "`nDone. Verify with: Get-ScheduledTask | Where TaskName -like '*IBKR*Live*' | Select TaskName,State"
