<#
.SYNOPSIS
    Avanza ISK sleeve dashboard — read-only monitor.

.DESCRIPTION
    Calls avanza_module/avanza_dashboard_helper.py to gather account state
    (balance, positions, open orders, recent trades, last signal) and formats
    it for the terminal.

    Never places orders. Safe to run at any time.

.PARAMETER Watch
    Poll and redraw every 60 seconds (-Watch). Without -Watch, prints once.

.EXAMPLE
    .\dashboard_avanza.ps1
    .\dashboard_avanza.ps1 -Watch
#>
param(
    [switch]$Watch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$ScriptRoot   = Split-Path -Parent $MyInvocation.MyCommand.Path
$HelperScript = Join-Path $ScriptRoot "avanza_module\avanza_dashboard_helper.py"

# ── Shared helpers ────────────────────────────────────────────────────────────
. (Join-Path $ScriptRoot "dashboard_common.ps1")

$Python = Find-Python $ScriptRoot
if (-not $Python) {
    Write-Host "ERROR: Python not found. Activate the .venv or install Python." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $HelperScript)) {
    Write-Host "ERROR: Helper not found: $HelperScript" -ForegroundColor Red
    exit 1
}

# ── Main draw ─────────────────────────────────────────────────────────────────
function Draw-Dashboard {
    $d = Invoke-PythonHelper $Python $HelperScript
    if ($null -eq $d) {
        Write-Host "  ERROR: avanza_dashboard_helper.py failed or returned invalid JSON." -ForegroundColor Red
        Write-Host "  Run:  $Python $HelperScript" -ForegroundColor DarkGray
        Write-Host "  Check that .env.avanza credentials are correct." -ForegroundColor DarkGray
        return
    }

    $now  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "-" * 68

    # ── Header ────────────────────────────────────────────────────────────────
    NL
    Write-Host ("  AVANZA ISK — US Blend Mirror{0,40}" -f $now) -ForegroundColor Cyan
    Write-Host "  $line" -ForegroundColor DarkGray
    if ($Watch) {
        Write-Host "  Auto-refresh every 60s  |  Ctrl+C to exit" -ForegroundColor DarkGray
        NL
    }

    # ── Connection status ─────────────────────────────────────────────────────
    if ($d.live_ok -eq $false) {
        $errMsg = if ($d.live_err) { $d.live_err } else { "unknown error" }
        Write-Host "  [!] Avanza connection FAILED: $errMsg" -ForegroundColor Red
        NL
    }

    # ── Open positions (top) ──────────────────────────────────────────────────
    $positions = @($d.positions)
    Write-Host ("  OPEN POSITIONS  ({0})" -f $positions.Count) -ForegroundColor Cyan
    Write-Host "  $("-" * 56)" -ForegroundColor DarkGray

    if ($positions.Count -gt 0) {
        Write-Host ("  {0,-8} {1,-20} {2,5} {3,8} {4,8} {5,10} {6,7}" -f `
            "Ticker", "Name", "Qty", "AvgCost", "Last", "~SEK", "Gain%") -ForegroundColor DarkGray
        Write-Host "  $("-" * 72)" -ForegroundColor DarkGray
        foreach ($pos in $positions) {
            $g    = if ($null -ne $pos.gain_pct) { [double]$pos.gain_pct } else { 0.0 }
            $gClr = if ($g -ge 0) { "Green" } else { "Red" }
            $gStr = if ($g -ge 0) { "+{0:F1}%" -f $g } else { "{0:F1}%" -f $g }
            $name = if ($pos.name) { $pos.name.ToString().Substring(0, [Math]::Min($pos.name.Length, 19)) } else { "" }
            Write-Host ("  {0,-8} {1,-20} {2,5} {3,8} " -f `
                $pos.ticker, $name, $pos.qty, ("{0:N2}" -f $pos.avg_price)) -NoNewline -ForegroundColor White
            Write-Host ("{0,8} " -f ("{0:N2}" -f $pos.current_price)) -NoNewline -ForegroundColor White
            Write-Host ("{0,10} " -f ("{0:N0} SEK" -f $pos.value_sek)) -NoNewline -ForegroundColor White
            Write-Host ("{0,7}" -f $gStr) -ForegroundColor $gClr
        }
    } else {
        Write-Host "  No open positions." -ForegroundColor DarkGray
    }
    NL

    # ── Account summary ───────────────────────────────────────────────────────
    Write-Host "  ACCOUNT" -ForegroundColor Cyan
    Write-Host "  $("-" * 40)" -ForegroundColor DarkGray
    if ($d.live_ok) {
        $acct = if ($d.account_type) { "($($d.account_type))" } else { "" }
        Write-Host ("  Account ID   : {0}  {1}" -f $d.account_id, $acct) -ForegroundColor White
        Write-Host ("  Total value  : {0,16}" -f (Fmt-Sek $d.value_sek)) -ForegroundColor White
        Write-Host ("  Buying power : {0,16}" -f (Fmt-Sek $d.buying_power_sek)) -ForegroundColor Green
        $gAcct = if ($null -ne $d.total_profit_pct) { [double]$d.total_profit_pct } else { 0.0 }
        $gAcctClr = if ($gAcct -ge 0) { "Green" } else { "Red" }
        Write-Host ("  Total profit : {0,16}" -f (Fmt-Pct $gAcct)) -ForegroundColor $gAcctClr
    } else {
        Write-Host "  (unavailable — connection failed)" -ForegroundColor DarkGray
    }

    $pnl = if ($null -ne $d.today_pnl_sek) { [double]$d.today_pnl_sek } else { 0.0 }
    $pnlClr = if ($pnl -ge 0) { "Green" } else { "Red" }
    Write-Host ("  Today P&L    : {0,16}" -f ("{0:+0;-0;0} SEK" -f [math]::Round($pnl))) `
        -ForegroundColor $pnlClr
    NL

    # ── Open orders ───────────────────────────────────────────────────────────
    $orders = @($d.open_orders)
    if ($orders.Count -gt 0) {
        Write-Host ("  OPEN ORDERS ({0})" -f $orders.Count) -ForegroundColor Cyan
        Write-Host "  $("-" * 40)" -ForegroundColor DarkGray
        foreach ($o in $orders) {
            $sClr = if ($o.side -eq "BUY") { "Green" } else { "Red" }
            Write-Host ("  {0,-4} {1,-8} qty={2,4}  @ {3}" -f `
                $o.side, $o.ticker, $o.qty, $o.price) -ForegroundColor $sClr
        }
        NL
    }

    # ── Last signal ───────────────────────────────────────────────────────────
    if ($d.signal_tickers -and $d.signal_tickers.Count -gt 0) {
        $ago     = if ($null -ne $d.last_run_mins_ago) { Fmt-MinAgo $d.last_run_mins_ago } else { "--" }
        $source  = if ($d.signal_source) { $d.signal_source } else { "unknown" }
        $budgStr = if ($null -ne $d.budget_sek) { "{0:N0} SEK" -f [double]$d.budget_sek } else { "--" }
        Write-Host "  SIGNAL  (from ATOS US Blend scan)" -ForegroundColor Cyan
        Write-Host "  $("-" * 40)" -ForegroundColor DarkGray
        Write-Host ("  Last run     : {0}  ({1})" -f $d.last_run, $ago) -ForegroundColor DarkGray
        Write-Host ("  Source       : {0}" -f $source) -ForegroundColor DarkGray
        Write-Host ("  Budget       : {0}" -f $budgStr) -ForegroundColor DarkGray
        Write-Host ("  Target basket: {0}" -f ($d.signal_tickers -join "  ")) -ForegroundColor White
        NL
    }

    # ── Recent trades ─────────────────────────────────────────────────────────
    $trades = @($d.recent_trades)
    if ($trades.Count -gt 0) {
        Write-Host ("  RECENT TRADES (last {0})" -f $trades.Count) -ForegroundColor Cyan
        Write-Host "  $("-" * 68)" -ForegroundColor DarkGray
        Write-Host ("  {0,-10} {1,-4} {2,-8} {3,5} {4,8} {5,12}" -f `
            "Date", "Side", "Ticker", "Qty", "Price", "P&L (SEK)") -ForegroundColor DarkGray
        Write-Host "  $("-" * 62)" -ForegroundColor DarkGray
        foreach ($t in $trades) {
            $side    = if ($t.side) { $t.side } else { "?" }
            $sideClr = if ($side -eq "BUY") { "Cyan" } else { "Yellow" }
            $pnlT    = if ($null -ne $t.pnl_sek) { [double]$t.pnl_sek } else { 0.0 }
            $pnlClrT = if ($pnlT -ge 0) { "Green" } else { "Red" }
            $pnlStr  = if ($t.side -eq "SELL" -or $null -ne $t.pnl_sek) {
                "{0:+0;-0} SEK" -f [math]::Round($pnlT) } else { "--" }
            $date    = if ($t.entry_date) { $t.entry_date.ToString().Substring(0,10) } else { "--" }
            Write-Host ("  {0,-10} " -f $date) -NoNewline -ForegroundColor DarkGray
            Write-Host ("{0,-4} " -f $side) -NoNewline -ForegroundColor $sideClr
            Write-Host ("{0,-8} {1,5} {2,8} " -f `
                $t.ticker, $t.qty, ("{0:N2}" -f $t.price)) -NoNewline -ForegroundColor White
            Write-Host ("{0,12}" -f $pnlStr) -ForegroundColor $pnlClrT
        }
        NL
    }

    Write-Host "  $line" -ForegroundColor DarkGray
    Write-Host "  READ-ONLY: this dashboard never places orders on Avanza." -ForegroundColor DarkGray
    NL
}

# ── Entry point ───────────────────────────────────────────────────────────────
if ($Watch) {
    while ($true) {
        Clear-Host
        Draw-Dashboard
        Write-Host "  Next refresh in 60s...  (Ctrl+C to exit)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 60
    }
} else {
    Draw-Dashboard
}
