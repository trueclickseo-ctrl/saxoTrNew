# Creates 3 IBKR Live retry tasks (exits / entries / blend) 23 min after the main tasks.
# Run once as Administrator.

$principal = New-ScheduledTaskPrincipal -UserId "Kwaseem" -LogonType Interactive -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable

$tasks = @(
    @{
        Name    = "ATOS IBKR Reversion Live Exits RETRY"
        Bat     = "run_ibkr_reversion_live_exits.bat"
        Log     = "ibkr_reversion_live_exits_retry.log"
        Time    = "19:53"
    },
    @{
        Name    = "ATOS IBKR Reversion Live Entries RETRY"
        Bat     = "run_ibkr_reversion_live.bat"
        Log     = "ibkr_reversion_live_retry.log"
        Time    = "20:23"
    },
    @{
        Name    = "ATOS IBKR Blend Rebalance RETRY"
        Bat     = "run_ibkr_blend_live.bat"
        Log     = "ibkr_blend_live_rebalance_retry.log"
        Time    = "20:52"
    }
)

$root = "E:\SaxoTrNew\SaxoTrNew"
$vbs  = "$root\run_hidden.vbs"

foreach ($t in $tasks) {
    $arg = "`"$vbs`" `"$root\$($t.Bat)`" `"$root\data\$($t.Log)`""
    $action  = New-ScheduledTaskAction -Execute "wscript.exe" -Argument $arg
    $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
    try {
        Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
            -Settings $settings -Principal $principal -Force -ErrorAction Stop
        Write-Host "OK: $($t.Name)"
    } catch {
        Write-Host "FAILED: $($t.Name) -- $_"
    }
}
Write-Host "Done."
