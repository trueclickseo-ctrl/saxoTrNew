<#
.SYNOPSIS
    Saxo SIM bot dashboard -- read-only monitor.

.DESCRIPTION
    Calls bots/saxo_dashboard_helper.py to gather bot state (balance,
    positions, recent trades, today's scan signals, bot health) and
    formats it for the terminal.

    Market-hours aware: the ATOS bot runs once per day after US close,
    so a gap in the log since yesterday is expected on weekends and during
    pre-market / after-hours on weekdays.

    Never places orders, cancels orders, or writes any files.

.PARAMETER Watch
    Poll and redraw every 60 seconds (-Watch). Without -Watch, prints once.

.EXAMPLE
    .\dashboard_saxo.ps1
    .\dashboard_saxo.ps1 -Watch
#>
param(
    [switch]$Watch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$ScriptRoot   = Split-Path -Parent $MyInvocation.MyCommand.Path
$HelperScript = Join-Path $ScriptRoot "bots\saxo_dashboard_helper.py"

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
        Write-Host "  ERROR: saxo_dashboard_helper.py failed or returned invalid JSON." -ForegroundColor Red
        Write-Host "  Check: $Python $HelperScript" -ForegroundColor DarkGray
        return
    }

    $now  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "-" * 68

    # ── Header ────────────────────────────────────────────────────────────────
    NL
    Write-Host ("  SAXO SIM BOT -- DASHBOARD{0,44}" -f $now) -ForegroundColor Cyan
    Write-Host "  $line" -ForegroundColor DarkGray
    if ($Watch) {
        Write-Host "  Auto-refresh every 60s  |  Ctrl+C to exit" -ForegroundColor DarkGray
        NL
    }

    if ($d.error) {
        Write-Host "  [ERROR] $($d.error)" -ForegroundColor Red
        NL
    }

    # ── Market status ─────────────────────────────────────────────────────────
    $mkt = $d.market
    if ($mkt) {
        $mktClr = if ($mkt.open) { "Green" } else { "DarkGray" }
        Write-Host ("  Market  :  {0}" -f $mkt.status) -ForegroundColor $mktClr
    }

    # ── Account balance ───────────────────────────────────────────────────────
    NL
    Write-Host "  ACCOUNT" -ForegroundColor Cyan
    Write-Host "  $("-" * 40)" -ForegroundColor DarkGray
    if ($d.balance) {
        $bal = $d.balance
        $src = if ($bal.source -eq "live") { "(live)" } elseif ($bal.source -eq "db_last_snapshot") { "(last DB snapshot)" } else { "(estimate)" }
        Write-Host ("  Total equity  : {0,16}  {1}" -f (Fmt-Sek $bal.total_sek), $src) -ForegroundColor White
        if ($null -ne $bal.cash_sek) {
            Write-Host ("  Cash available: {0,16}" -f (Fmt-Sek $bal.cash_sek)) -ForegroundColor Green
        }
        if ($null -ne $bal.margin_sek) {
            Write-Host ("  Margin avail  : {0,16}" -f (Fmt-Sek $bal.margin_sek)) -ForegroundColor Yellow
        }
    } else {
        Write-Host "  (unavailable)" -ForegroundColor DarkGray
    }
    NL

    # ── Open positions ────────────────────────────────────────────────────────
    $slots = $d.slots
    $blendN = if ($slots -and $null -ne $slots.blend.open) { $slots.blend.open } else { 0 }
    $revN   = if ($slots -and $null -ne $slots.reversion.open) { $slots.reversion.open } else { 0 }

    Write-Host ("  OPEN POSITIONS  (Blend: {0}  |  Reversion: {1})" -f $blendN, $revN) -ForegroundColor Cyan
    Write-Host "  $("-" * 56)" -ForegroundColor DarkGray

    $positions = @($d.positions)
    if ($positions.Count -gt 0) {
        Write-Host ("  {0,-8} {1,-10} {2,-14} {3,10} {4,8} {5,8}" -f `
            "Strategy", "Ticker", "Entry date", "Shares", "Entry$", "Days") -ForegroundColor DarkGray
        Write-Host "  $("-" * 68)" -ForegroundColor DarkGray
        foreach ($pos in $positions) {
            $strat  = if ($pos.strategy) { $pos.strategy } else { "?" }
            $ticker = if ($pos.ticker)   { $pos.ticker }   else { if ($pos.symbol) { $pos.symbol } else { "?" } }
            $entry  = if ($pos.entry_price -or $pos.OpenPrice) { $pos.entry_price } else { "--" }
            $shares = if ($pos.shares -or $pos.net_qty) { if ($pos.shares) { $pos.shares } else { $pos.net_qty } } else { "--" }
            $days   = if ($null -ne $pos.days_open) { $pos.days_open } else { "--" }
            $eDate  = if ($pos.entry_date) { $pos.entry_date } else { "--" }
            $stratClr = if ($strat -like "*Reversion*") { "Yellow" } else { "Green" }
            Write-Host ("  {0,-8} " -f $strat.Replace("US ", "")) -NoNewline -ForegroundColor $stratClr
            Write-Host ("{0,-10} {1,-14} {2,10} {3,8} {4,8}d" -f `
                $ticker, $eDate, $shares, ("{0:N2}" -f $entry), $days) -ForegroundColor White
        }
    } else {
        Write-Host "  No open positions." -ForegroundColor DarkGray
    }
    NL

    # ── Recent closed trades ──────────────────────────────────────────────────
    Write-Host "  RECENT TRADES  (last 20 closed)" -ForegroundColor Cyan
    Write-Host "  $("-" * 68)" -ForegroundColor DarkGray

    $trades = @($d.recent_trades)
    if ($trades.Count -gt 0) {
        Write-Host ("  {0,-10} {1,-10} {2,-5} {3,9} {4,9} {5,12}  {6}" -f `
            "Exit date", "Ticker", "Side", "Entry$", "Exit$", "P&L (SEK)", "Strategy") -ForegroundColor DarkGray
        Write-Host "  $("-" * 78)" -ForegroundColor DarkGray
        foreach ($t in $trades) {
            $pnl     = if ($null -ne $t.pnl_sek) { $t.pnl_sek } else { 0 }
            $pnlClr  = if ($pnl -ge 0) { "Green" } else { "Red" }
            $pnlStr  = if ($null -ne $t.pnl_sek) { ("{0:+0;-0}  SEK" -f [math]::Round($pnl)) } else { "--" }
            $side    = if ($t.direction) { $t.direction } else { "?" }
            $sideClr = if ($side -eq "BUY") { "Cyan" } else { "Red" }
            $strat   = if ($t.strategy) { $t.strategy.Replace("US ", "") } else { "?" }
            Write-Host ("  {0,-10} {1,-10} " -f $t.exit_date, $t.ticker) -NoNewline -ForegroundColor White
            Write-Host ("{0,-5} " -f $side) -NoNewline -ForegroundColor $sideClr
            Write-Host ("{0,9} {1,9} " -f ("{0:N2}" -f $t.entry_price), ("{0:N2}" -f $t.exit_price)) -NoNewline -ForegroundColor White
            Write-Host ("{0,12}" -f $pnlStr) -NoNewline -ForegroundColor $pnlClr
            Write-Host ("  {0}" -f $strat) -ForegroundColor DarkGray
        }
    } else {
        Write-Host "  No closed trades yet." -ForegroundColor DarkGray
    }
    NL

    # ── Today's scan signals ──────────────────────────────────────────────────
    $ls = $d.last_scan
    if ($ls -and $ls.available) {
        $ago   = Fmt-MinAgo $ls.minutes_ago
        $label = if ($ls.scan_ts) { $ls.scan_ts.Substring(0,19).Replace("T"," ") + "  ($ago)" } else { "unknown" }
        Write-Host "  TODAY'S SCAN  --  $label" -ForegroundColor Cyan
        Write-Host "  $("-" * 68)" -ForegroundColor DarkGray

        $ss = $d.signals_summary
        if ($ss) {
            Write-Host ("  Reversion: {0} scanned  |  {1} BUY signals  |  {2} executed  |  {3} SKIP" -f `
                $ss.reversion_scanned, $ss.reversion_buy, $ss.reversion_executed, $ss.reversion_skip) -ForegroundColor White
            Write-Host ("  Blend:     {0} BUY targets" -f $ss.blend_buy) -ForegroundColor White
        }

        $strategies = $ls.strategies
        if ($strategies) {
            NL
            # US Blend
            $blend = $strategies."US Blend"
            if ($blend) {
                $blClr = if ($blend.risk_off) { "Red" } else { "Green" }
                Write-Host "  US Blend:" -NoNewline -ForegroundColor DarkGray
                Write-Host ("  {0}" -f $blend.status) -NoNewline -ForegroundColor $blClr
                if ($blend.targets -and $blend.targets.Count -gt 0) {
                    Write-Host ("  | targets: {0}" -f ($blend.targets -join ", ")) -ForegroundColor White
                } else {
                    NL
                }
                if ($blend.reason) {
                    Write-Host ("    Reason: {0}" -f $blend.reason) -ForegroundColor DarkGray
                }
                if ($null -ne $blend.days_since_rebalance) {
                    Write-Host ("    Days since last rebalance: {0}" -f $blend.days_since_rebalance) -ForegroundColor DarkGray
                }
            }
            NL

            # US Reversion top signals (BUY + near-miss SKIPs)
            Write-Host "  US Reversion signals:" -ForegroundColor DarkGray
            $revSigs = @($d.signals_today | Where-Object { $_.strategy -eq "US Reversion" })
            $revBuys = @($revSigs | Where-Object { $_.action -eq "BUY" } | Sort-Object -Property final_score -Descending)
            $revSkip = @($revSigs | Where-Object { $_.action -eq "SKIP" -and $null -ne $_.rsi -and $_.rsi -gt 0 -and $_.rsi -lt 40 } | Sort-Object -Property rsi | Select-Object -First 10)

            if ($revBuys.Count -eq 0 -and $revSkip.Count -eq 0) {
                Write-Host "  No BUY signals or near-misses today." -ForegroundColor DarkGray
            }

            foreach ($sig in $revBuys) {
                $rsiStr = if ($null -ne $sig.rsi)       { "RSI={0:F1}" -f $sig.rsi }       else { "RSI=?" }
                $dipStr = if ($null -ne $sig.dip_pct)   { "dip={0:F1}%" -f $sig.dip_pct }  else { "" }
                $volStr = if ($null -ne $sig.vol_ratio) { "vol={0:F2}x" -f $sig.vol_ratio } else { "" }
                $exeClr = if ($sig.executed) { "Green" } else { "Yellow" }
                $exeTag = if ($sig.executed) { "EXEC  " } else { "QUEUED" }
                $blk    = if ($sig.block_reason) { "  [$($sig.block_reason)]" } else { "" }
                Write-Host ("  {0,-10}  " -f $sig.ticker) -NoNewline -ForegroundColor White
                Write-Host ("{0}  " -f $exeTag) -NoNewline -ForegroundColor $exeClr
                Write-Host ("{0,-8} {1,-12} {2,-12}{3}" -f $rsiStr, $dipStr, $volStr, $blk) -ForegroundColor Green
            }

            foreach ($sig in $revSkip) {
                $rsiStr = if ($null -ne $sig.rsi)       { "RSI={0:F1}" -f $sig.rsi }       else { "RSI=?" }
                $dipStr = if ($null -ne $sig.dip_pct)   { "dip={0:F1}%" -f $sig.dip_pct }  else { "" }
                $volStr = if ($null -ne $sig.vol_ratio) { "vol={0:F2}x" -f $sig.vol_ratio } else { "" }
                Write-Host ("  {0,-10}  NEAR  {1,-8} {2,-12} {3,-12}" -f `
                    $sig.ticker, $rsiStr, $dipStr, $volStr) -ForegroundColor Yellow
            }
        }
    } else {
        Write-Host "  TODAY'S SCAN" -ForegroundColor Cyan
        Write-Host "  $("-" * 40)" -ForegroundColor DarkGray
        Write-Host "  No scan data for today -- atos_runner.py hasn't run yet or scan_state.json is missing." -ForegroundColor DarkGray
    }
    NL

    # ── Bot health ────────────────────────────────────────────────────────────
    Write-Host "  BOT HEALTH" -ForegroundColor Cyan
    Write-Host "  $("-" * 40)" -ForegroundColor DarkGray

    $h   = $d.health
    $mkt = $d.market
    if ($h) {
        $marketClosed = ($null -ne $mkt -and -not $mkt.open)

        $sClr = switch ($h.status) {
            "RAN_TODAY"     { "Green" }
            "RAN_YESTERDAY" { if ($marketClosed) { "DarkGray" } else { "Yellow" } }
            default         { "Red" }
        }
        Write-Host "  Status        :  " -NoNewline -ForegroundColor DarkGray
        Write-Host $h.status -ForegroundColor $sClr

        if ($h.last_scan_date) {
            Write-Host ("  Last run date :  {0}" -f $h.last_scan_date) -ForegroundColor DarkGray
        }
        if ($null -ne $h.log_age_minutes) {
            $ageStr = Fmt-MinAgo $h.log_age_minutes
            # Staleness: flag only during market hours or if >48h old
            $stale  = ($h.log_age_minutes -gt 1440 -and -not $marketClosed) -or $h.log_age_minutes -gt 2880
            $ageClr = if ($stale) { "Red" } elseif ($marketClosed) { "DarkGray" } else { "Green" }
            Write-Host "  Log age       :  " -NoNewline -ForegroundColor DarkGray
            Write-Host $ageStr -ForegroundColor $ageClr
        }
        if ($h.log_file) {
            Write-Host ("  Log file      :  {0}" -f $h.log_file) -ForegroundColor DarkGray
        }

        if ($marketClosed -and $h.status -ne "RAN_TODAY") {
            Write-Host "  [ok] Market is closed -- gap in scan log is expected." -ForegroundColor DarkGray
        }
        if ($h.status -eq "UNKNOWN") {
            Write-Host "  [!] No engine log found -- has atos_runner.py ever run?" -ForegroundColor Red
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
        Write-Host "  Next refresh in 60s...  (Ctrl+C to exit)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 60
    }
} else {
    Draw-Dashboard
}
