# setup_ibkr_gateway_watchdog.ps1
# Registers "ATOS IBKR Gateway Watchdog" — runs every 5 minutes, checks
# IB Gateway on port 4001 and auto-restarts it if it goes down.
#
# Run once from an elevated PowerShell prompt:
#   powershell -ExecutionPolicy Bypass -File setup_ibkr_gateway_watchdog.ps1

$TaskName  = "ATOS IBKR Gateway Watchdog"
$ScriptDir = "E:\SaxoTrNew\SaxoTrNew"
$Vbs       = "$ScriptDir\run_hidden.vbs"
$Bat       = "$ScriptDir\run_ibkr_gateway_watchdog.bat"
$Log       = "$ScriptDir\data\ibkr_gateway_watchdog.log"

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Monitors IB Gateway on port 4001 and auto-restarts it if down. Runs every 5 minutes. Part of ATOS IBKR watchdog stack.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-09-16T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT4M</ExecutionTimeLimit>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions>
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>"$Vbs" "$Bat" "$Log"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$XmlPath = "$env:TEMP\ibkr_gw_watchdog.xml"
[System.IO.File]::WriteAllText($XmlPath, $xml, [System.Text.Encoding]::Unicode)

schtasks.exe /Create /TN $TaskName /XML $XmlPath /F
Remove-Item $XmlPath -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Verifying..."
schtasks.exe /Query /TN $TaskName /FO LIST
