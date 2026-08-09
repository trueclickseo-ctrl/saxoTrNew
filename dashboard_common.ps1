#
# dashboard_common.ps1
# ---------------------
# Shared formatting helpers dot-sourced by dashboard_binance.ps1 and
# dashboard_saxo.ps1.  Keep broker-specific logic out of this file.
#

function NL { Write-Host "" }

function Fmt-Usdt([double]$v)   { return ("{0:N2}" -f $v) }
function Fmt-Sek([double]$v)    { return ("{0:N0} SEK" -f $v) }
function Fmt-Pct([double]$v)    { return ("{0:+0.00;-0.00;0.00}%" -f $v) }
function Fmt-MinAgo([object]$m) {
    if ($null -eq $m) { return "--" }
    $mins = [double]$m
    if ($mins -lt 1)   { return "just now" }
    if ($mins -lt 60)  { return ("{0:0}m ago" -f $mins) }
    if ($mins -lt 1440){ return ("{0:0}h {1:0}m ago" -f [math]::Floor($mins / 60), ($mins % 60)) }
    return ("{0:0}d {1:0}h ago" -f [math]::Floor($mins / 1440), [math]::Floor(($mins % 1440) / 60))
}

function Find-Python {
    param([string]$ScriptRoot)
    $venv = Join-Path $ScriptRoot ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Invoke-PythonHelper {
    param(
        [string]$Python,
        [string]$HelperScript
    )
    $raw = & $Python $HelperScript 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
    try {
        $jsonStr = ($raw -join "`n").TrimStart([char]0xFEFF)
        return $jsonStr | ConvertFrom-Json
    } catch {
        return $null
    }
}
