# setup_scheduler_ibkr_intraday.ps1
# ------------------------------------
# Registers "ATOS IBKR Intraday Reversion" in Windows Task Scheduler.
# Fires 5x during the US session (mirrors the ATOS SIM intraday scan cadence):
#
#   19:00 PKT (10:00 ET) -- 30 min after open, dust settled
#   20:30 PKT (11:30 ET) -- midday
#   22:00 PKT (13:00 ET) -- early afternoon
#   23:30 PKT (14:30 ET) -- mid afternoon
#   00:30 PKT (15:30 ET) -- pre-close (00:30 = next calendar day in PKT)
#
# Uses 5-minute yfinance bars for live price. Dry-run only -- logs signals to
# ibkr_intraday.log. Execute intraday entries manually with:
#   python run_ibkr_stocks.py --strategy intraday --execute
#
# Requires IB Gateway running during US hours.
# Watchdog key: "IBKR Intraday Reversion" -> max_log_age_hours=2 (intraday repeating)
#
# RUN ONCE AS ADMINISTRATOR:
#   powershell -ExecutionPolicy Bypass -File "E:\SaxoTrNew\SaxoTrNew\setup_scheduler_ibkr_intraday.ps1"

$vbs     = "E:\SaxoTrNew\SaxoTrNew\run_hidden.vbs"
$base    = "E:\SaxoTrNew\SaxoTrNew"
$logFile = "$base\data\ibkr_intraday.log"

$settings = New-ScheduledTaskSettingsSet `
            -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
            -StartWhenAvailable -WakeToRun `
            -RestartCount 1 -RestartInterval (New-TimeSpan -Minutes 5)

$action = New-ScheduledTaskAction -Execute "wscript.exe" `
          -Argument ('"' + $vbs + '" "' + $base + '\run_ibkr_intraday.bat" "' + $logFile + '"')

# Five daily triggers -- Windows fires all five each calendar day
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At "19:00"),
    (New-ScheduledTaskTrigger -Daily -At "20:30"),
    (New-ScheduledTaskTrigger -Daily -At "22:00"),
    (New-ScheduledTaskTrigger -Daily -At "23:30"),
    (New-ScheduledTaskTrigger -Daily -At "00:30")
)

try {
    Register-ScheduledTask -TaskName "ATOS IBKR Intraday Reversion" `
               -Action $action -Trigger $triggers -Settings $settings `
               -Description "IBKR: intraday reversion scan 5x/day (5-min bars). 19:00-00:30 PKT / 10:00-15:30 ET." `
               -RunLevel Highest -Force
    Write-Host "OK  Registered: ATOS IBKR Intraday Reversion -> 19:00/20:30/22:00/23:30/00:30 PKT"
} catch {
    Write-Host "FAIL $($_.Exception.Message)"
}
