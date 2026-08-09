<#
.SYNOPSIS
    Binance testnet bot dashboard -- read-only monitor.

.DESCRIPTION
    Calls bots/dashboard_helper.py to gather bot state (account, positions,
    recent trades, last scan, bot health) and formats it for the terminal.

    Never places orders, cancels orders, or writes any files.

.PARAMETER Watch
    Poll and redraw every 30 seconds (like 'watch' on Linux).
    Without -Watch, prints once and exits.

.EXAMPLE
    .\dashboard_binance.ps1
    .\dashboard_binance.ps1 -Watch

#>
param(
    [switch]$Watch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$ScriptRoot   = Split-Path -Parent $MyInvocation.MyCommand.Path
$HelperScript = Join-Path $ScriptRoot "bots\dashboard_helper.py"

# ── Shared helpers ────────────────────────────────────────────────────────────
. (Join-Path $ScriptRoot "dashboard_common.ps1")

$Python = Find-Python $ScriptRoot
if (-not $Python) {
    Write-Host "ERROR: Python not found. Activate the .venv or install Python." -ForegroundColor Red
    exit 1
}

# ── Main draw ─────────────────────────────────────────────────────────────────
function Draw-Dashboard {
    $d = Invoke-PythonHelper $Python $HelperScript
    if ($null -eq $d) {
        Write-Host "  ERROR: dashboard_helper.py failed or returned invalid JSON." -ForegroundColor Red
        Write-Host "  Check: $Python $HelperScript" -ForegroundColor DarkGray
        return
    }

    $now  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "-" * 68

    # ── Header ────────────────────────────────────────────────────────────────
    NL
    Write-Host ("  BINANCE TESTNET BOT -- DASHBOARD{0,36}" -f $now) -ForegroundColor Cyan
    Write-Host "  $line" -ForegroundColor DarkGray
    if ($Watch) {
        Write-Host "  Auto-refresh every 30s  |  Ctrl+C to exit" -ForegroundColor DarkGray
        NL
    }

    # API error banner
    if ($d.error) {
        Write-Host "  [API ERROR] $($d.error)" -ForegroundColor Red
        NL
    }

    # ── Account ───────────────────────────────────────────────────────────────
    Write-Host "  ACCOUNT" -ForegroundColor Cyan
    Write-Host "  $("-" * 40)" -ForegroundColor DarkGray
    if ($d.account) {
        $a = $d.account
        Write-Host ("  Total USDT  :{0,14}" -f (Fmt-Usdt $a.total_usdt))  -ForegroundColor White
        Write-Host ("  Free  USDT  :{0,14}" -f (Fmt-Usdt $a.free_usdt))   -ForegroundColor Green
        Write-Host ("  Locked USDT :{0,14}" -f (Fmt-Usdt $a.locked_usdt)) -ForegroundColor Yellow
    } else {
        Write-Host "  (unavailable -- API error)" -ForegroundColor DarkGray
    }
    NL

    # ── Positions + slots ─────────────────────────────────────────────────────
    if ($d.slots) {
        $sl = $d.slots
        Write-Host ("  POSITIONS  ({0} / {1} slots used)" -f $sl.used, $sl.max) -ForegroundColor Cyan
    } else {
        Write-Host "  POSITIONS" -ForegroundColor Cyan
    }
    Write-Host "  $("-" * 40)" -ForegroundColor DarkGray

    if ($d.positions -and $d.positions.Count -gt 0) {
        foreach ($pos in $d.positions) {
            $pnlColor = if ($pos.unrealized_pnl -ge 0) { "Green" } else { "Red" }
            $sign     = if ($pos.unrealized_pnl -ge 0) { "+" }     else { "" }
            Write-Host ("  {0,-10}  qty={1,-14}  entry={2,10}  now={3,10}  P&L={4}{5} ({6})" -f `
                $pos.symbol,
                ("{0:0.########}" -f $pos.net_qty),
                ("$" + (Fmt-Usdt $pos.avg_entry)),
                ("$" + (Fmt-Usdt $pos.current_price)),
                $sign,
                (Fmt-Usdt $pos.unrealized_pnl),
                (Fmt-Pct  $pos.unrealized_pnl_pct)) -ForegroundColor $pnlColor
        }
    } else {
        Write-Host "  No open positions (from our trade history)." -ForegroundColor DarkGray
    }

    if ($d.slots -and $d.slots.free_symbols.Count -gt 0) {
        Write-Host ("  Watching free slots: {0}" -f ($d.slots.free_symbols -join ", ")) -ForegroundColor DarkGray
    }
    NL

    # ── Recent trades ─────────────────────────────────────────────────────────
    Write-Host "  RECENT TRADES  (last 20 fills)" -ForegroundColor Cyan
    Write-Host "  $("-" * 68)" -ForegroundColor DarkGray

    if ($d.recent_trades -and $d.recent_trades.Count -gt 0) {
        Write-Host ("  {0,-22} {1,-10} {2,-5} {3,12} {4,12}  {5}" -f `
            "Time (UTC)", "Symbol", "Side", "Price", "Qty", "Strategy") -ForegroundColor DarkGray
        Write-Host "  $("-" * 78)" -ForegroundColor DarkGray
        foreach ($t in $d.recent_trades) {
            $ts      = if ($t.ts) { $t.ts.Substring(0, 19).Replace("T", " ") } else { "--" }
            $clr     = if ($t.side -eq "BUY") { "Green" } else { "Red" }
            $strat   = if ($t.strategy -eq "unknown") { "(pre-logging)" } else { $t.strategy }
            Write-Host ("  {0,-22} {1,-10} " -f $ts, $t.symbol) -NoNewline -ForegroundColor White
            Write-Host ("{0,-5} " -f $t.side) -NoNewline -ForegroundColor $clr
            Write-Host ("{0,12} {1,12}  {2}" -f `
                ("{0:N4}" -f $t.price),
                ("{0:0.########}" -f $t.qty),
                $strat) -ForegroundColor DarkGray
        }
    } else {
        Write-Host "  No fills found for universe symbols." -ForegroundColor DarkGray
    }
    NL

    # ── Last scan ─────────────────────────────────────────────────────────────
    $ls = $d.last_scan
    if ($ls -and $ls.available) {
        $ago   = Fmt-MinAgo $ls.minutes_ago
        $label = if ($ls.ts) { $ls.ts.Substring(0,19).Replace("T"," ") + " UTC  ($ago)" } else { "unknown" }
        Write-Host "  LAST SCAN  --  $label" -ForegroundColor Cyan
        Write-Host "  $("-" * 68)" -ForegroundColor DarkGray

        $sigs    = @($ls.signals)
        $buys    = @($sigs | Where-Object { $_.action -eq "BUY"    }).Count
        $queued  = @($sigs | Where-Object { $_.action -eq "QUEUED" }).Count
        $skipped = @($sigs | Where-Object { $_.action -eq "SKIP"   }).Count
        Write-Host ("  Scanned: {0} symbols  |  BUY: {1}  QUEUED: {2}  SKIP: {3}" -f `
            $sigs.Count, $buys, $queued, $skipped) -ForegroundColor White
        NL

        foreach ($sig in $sigs) {
            $rsiStr = if ($null -ne $sig.rsi)       { "RSI={0:F1}" -f $sig.rsi }       else { "RSI=?" }
            $dipStr = if ($null -ne $sig.dip_pct)   { "dip={0:F1}%" -f $sig.dip_pct } else { "" }
            $volStr = if ($null -ne $sig.vol_ratio) { "vol={0:F2}x" -f $sig.vol_ratio } else { "" }

            $clr = switch ($sig.action) {
                "BUY"    { "Green" }
                "QUEUED" { "Yellow" }
                default  { "DarkGray" }
            }
            $tag = switch ($sig.action) {
                "BUY"    { "BUY   " }
                "QUEUED" { "QUEUED" }
                default  { "SKIP  " }
            }

            # Near-miss: RSI < 45 but still SKIP (exclude data-gap skips)
            $nearMiss = ($sig.action -eq "SKIP" -and $null -ne $sig.rsi -and $sig.rsi -lt 45 `
                         -and $sig.rsi -gt 0 -and $sig.reason -notlike "*insufficient*")

            Write-Host "  " -NoNewline
            Write-Host ("{0,-10} " -f $sig.symbol) -NoNewline -ForegroundColor White
            Write-Host ("{0}  " -f $tag) -NoNewline -ForegroundColor $clr

            if ($nearMiss) {
                Write-Host ("{0,-8} {1,-12} {2,-12}" -f $rsiStr, $dipStr, $volStr) -NoNewline -ForegroundColor Yellow
                if ($sig.reason) { Write-Host ("  <- {0}" -f $sig.reason) -NoNewline -ForegroundColor Yellow }
            } else {
                Write-Host ("{0,-8} {1,-12} {2,-12}" -f $rsiStr, $dipStr, $volStr) -NoNewline -ForegroundColor DarkGray
                if ($sig.reason -and $sig.action -eq "SKIP") {
                    Write-Host ("  {0}" -f $sig.reason) -NoNewline -ForegroundColor DarkGray
                }
            }
            NL
        }
    } else {
        Write-Host "  LAST SCAN" -ForegroundColor Cyan
        Write-Host "  $("-" * 40)" -ForegroundColor DarkGray
        Write-Host "  No scan data yet -- run the bot once to populate binance_signals.jsonl." -ForegroundColor DarkGray
        NL
    }

    # ── Bot health ────────────────────────────────────────────────────────────
    Write-Host "  BOT HEALTH" -ForegroundColor Cyan
    Write-Host "  $("-" * 40)" -ForegroundColor DarkGray

    $h = $d.health
    if ($h) {
        $sClr = switch ($h.status) {
            "RUNNING"   { "Green" }
            "STOPPED"   { "DarkGray" }
            "STALE"     { "Red" }
            "PID_STALE" { "Yellow" }
            default     { "Yellow" }
        }
        Write-Host "  Loop status   : " -NoNewline -ForegroundColor DarkGray
        Write-Host ("{0,-12}" -f $h.status) -NoNewline -ForegroundColor $sClr
        if ($h.pid) { Write-Host ("  (PID {0})" -f $h.pid) -ForegroundColor DarkGray } else { NL }

        if ($null -ne $h.log_age_minutes) {
            $ageStr  = Fmt-MinAgo $h.log_age_minutes
            $ival    = $h.scan_interval_minutes
            $ageClr  = if ($h.log_age_minutes -gt $ival * 2) { "Red" } `
                       elseif ($h.log_age_minutes -gt $ival)  { "Yellow" } `
                       else { "Green" }
            Write-Host "  Last activity : " -NoNewline -ForegroundColor DarkGray
            Write-Host ("{0}  (interval: {1}m)" -f $ageStr, $ival) -ForegroundColor $ageClr
        }

        Write-Host "  Log file      : " -NoNewline -ForegroundColor DarkGray
        Write-Host $h.log_file -ForegroundColor DarkGray

        if ($h.status -eq "PID_STALE") {
            Write-Host "  [!] PID file exists but process gone -- bot may have crashed." -ForegroundColor Yellow
            Write-Host "      Check logs for errors before restarting." -ForegroundColor Yellow
        }
        if ($h.status -eq "STALE") {
            Write-Host "  [!] Loop may be hanging -- no log activity in >2x scan interval." -ForegroundColor Red
        }
    }
    NL
    Write-Host "  $line" -ForegroundColor DarkGray
    Write-Host "  READ-ONLY: this dashboard never places orders or touches the kill switch." -ForegroundColor DarkGray
    NL
}

# ── Entry point ───────────────────────────────────────────────────────────────
if ($Watch) {
    while ($true) {
        Clear-Host
        Draw-Dashboard
        Write-Host "  Next refresh in 30s..." -ForegroundColor DarkGray
        Start-Sleep -Seconds 30
    }
} else {
    Draw-Dashboard
}
