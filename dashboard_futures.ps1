<#
.SYNOPSIS
    Futures Trading Desk -- dashboard + optional live order placement.

.DESCRIPTION
    Reads futures_state.json, trades_futures.csv and live Saxo prices to
    show all 6 strategies, open positions, recent trades and log health.

    -Trade  : place live orders (runs futures/runner.py --live)
    -Scan   : show 6-panel market snapshot (no orders)
    -Watch  : auto-refresh monitor every 60s (read-only, no orders)
    -Dry    : test run -- shows signals but does NOT place real orders

.EXAMPLE
    .\dashboard_futures.ps1              # one-shot status
    .\dashboard_futures.ps1 -Watch       # live monitor (Ctrl+C to exit)
    .\dashboard_futures.ps1 -Scan        # 6-panel market scan
    .\dashboard_futures.ps1 -Dry         # dry-run: show what would trade
    .\dashboard_futures.ps1 -Trade       # ** PLACES REAL ORDERS **
#>
param(
    [switch]$Watch,
    [switch]$Trade,
    [switch]$Dry,
    [switch]$Scan
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Helper     = Join-Path $ScriptRoot "futures\status_helper.py"
$Runner     = Join-Path $ScriptRoot "futures\runner.py"
$LogDir     = Join-Path $ScriptRoot "logs"
$TradeLog   = Join-Path $ScriptRoot "data\trades_futures.csv"

. (Join-Path $ScriptRoot "dashboard_common.ps1")

$Python = Find-Python $ScriptRoot
if (-not $Python) {
    Write-Host "ERROR: Python not found." -ForegroundColor Red
    exit 1
}

function PnlColor([object]$v) {
    if ($null -eq $v) { return "DarkGray" }
    if ([double]$v -ge 0) { return "Green" } else { return "Red" }
}

function SideColor([string]$s) {
    if ($s -eq "Buy" -or $s -eq "BUY") { return "Cyan" } else { return "Magenta" }
}

function StratColor([string]$s) {
    switch ($s.ToLower()) {
        "donchian" { return "Blue"    }
        "rsi"      { return "Magenta" }
        "ema"      { return "Green"   }
        "macd"     { return "Yellow"  }
        "squeeze"  { return "Red"     }
        "ma_cross" { return "Cyan"    }
        # 2026-08-28 fix: was missing entirely -- trend_ma (futures/runner.py's
        # 7th strategy) fell through to the generic "White" default, same as
        # any truly-unknown strategy name, instead of getting its own color
        # once it eventually has a closed trade to show here.
        "trend_ma" { return "DarkYellow" }
        default    { return "White"   }
    }
}

function Draw-Dashboard {
    $raw = & $Python -X utf8 $Helper 2>$null
    $d   = $null
    if ($raw) {
        try { $d = ($raw -join "`n").TrimStart([char]0xFEFF) | ConvertFrom-Json }
        catch { }
    }

    $now  = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "-" * 72

    NL
    Write-Host "  FUTURES TRADING DESK" -NoNewline -ForegroundColor Cyan
    Write-Host ("{0,51}" -f $now) -ForegroundColor DarkGray
    Write-Host "  $line" -ForegroundColor DarkGray

    if ($Watch) {
        Write-Host "  Monitor mode  |  Ctrl+C to exit" -ForegroundColor DarkGray
        NL
    }

    if ($null -eq $d) {
        Write-Host "  [!] Could not load futures data. Check: $Helper" -ForegroundColor Red
        NL
        return
    }

    NL
    Write-Host "  ACCOUNT" -ForegroundColor Cyan
    Write-Host "  $("-" * 44)" -ForegroundColor DarkGray

    $eq = if ($null -ne $d.equity) { ("{0:N0}" -f [double]$d.equity) } else { "n/a" }
    Write-Host ("  Equity        :  {0,18}" -f $eq) -ForegroundColor White
    $used = $d.total_slots - $d.open_slots
    Write-Host ("  Slots         :  {0} open  /  {1} used  /  {2} total" -f $d.open_slots, $used, $d.total_slots) -ForegroundColor White
    NL

    $positions = @($d.positions)
    Write-Host ("  OPEN POSITIONS  [{0} / {1} slots used]" -f $positions.Count, $d.total_slots) -ForegroundColor Cyan
    Write-Host "  $("-" * 72)" -ForegroundColor DarkGray

    if ($positions.Count -eq 0) {
        Write-Host "  No open positions." -ForegroundColor DarkGray
    } else {
        Write-Host ("  {0,-10} {1,-5} {2,-5} {3,8} {4,12} {5,12} {6,9} {7,12}  {8}" -f `
            "Strategy","Sym","Side","Qty","Entry","Live","PnL %","Stop","Days") -ForegroundColor DarkGray
        Write-Host "  $("-" * 90)" -ForegroundColor DarkGray

        foreach ($pos in $positions) {
            $pnl    = if ($null -ne $pos.pnl_pct) { ("{0:+0.00;-0.00}%" -f [double]$pos.pnl_pct) } else { "n/a" }
            $live   = if ($null -ne $pos.live_price) { ("{0:N4}" -f [double]$pos.live_price) } else { "n/a" }
            $stop   = if ($null -ne $pos.stop_price) { ("{0:N4}" -f [double]$pos.stop_price) } else { "n/a" }
            $pnlC   = PnlColor $pos.pnl_pct
            $sideC  = SideColor $pos.direction
            $stratC = StratColor $pos.strategy

            Write-Host ("  {0,-10} " -f $pos.strategy.ToUpper()) -NoNewline -ForegroundColor $stratC
            Write-Host ("{0,-5} " -f $pos.symbol) -NoNewline -ForegroundColor White
            Write-Host ("{0,-5} " -f $pos.direction) -NoNewline -ForegroundColor $sideC
            Write-Host ("{0,8} {1,12} {2,12} " -f $pos.quantity, ("{0:N4}" -f [double]$pos.entry_price), $live) -NoNewline -ForegroundColor White
            Write-Host ("{0,9}" -f $pnl) -NoNewline -ForegroundColor $pnlC
            Write-Host (" {0,12}  {1}d" -f $stop, $pos.days_open) -ForegroundColor DarkGray
        }
    }
    NL

    $trades = @($d.trades)
    Write-Host "  RECENT TRADES  (last 15 from trades_futures.csv)" -ForegroundColor Cyan
    Write-Host "  $("-" * 72)" -ForegroundColor DarkGray

    if ($trades.Count -eq 0) {
        Write-Host "  No trades logged yet." -ForegroundColor DarkGray
    } else {
        Write-Host ("  {0,-19} {1,-6} {2,-10} {3,-5} {4,10} {5,6}  {6}" -f `
            "Timestamp","Strat","Symbol","Side","Price","Mode","Notes") -ForegroundColor DarkGray
        Write-Host "  $("-" * 76)" -ForegroundColor DarkGray

        foreach ($t in $trades) {
            $modeC = if ($t.mode -eq "LIVE") { "Green" } else { "Yellow" }
            $sideC = SideColor $t.side
            $ts    = if ($t.timestamp.Length -ge 16) { $t.timestamp.Substring(0,16) } else { $t.timestamp }
            Write-Host ("  {0,-19} {1,-6} {2,-10} " -f $ts, $t.strategy, $t.symbol) -NoNewline -ForegroundColor White
            Write-Host ("{0,-5} " -f $t.side) -NoNewline -ForegroundColor $sideC
            Write-Host ("{0,10} " -f $t.price) -NoNewline -ForegroundColor White
            Write-Host ("{0,6}" -f $t.mode) -NoNewline -ForegroundColor $modeC
            if ($t.notes) {
                Write-Host ("  {0}" -f $t.notes) -ForegroundColor DarkGray
            } else { NL }
        }
    }
    NL

    # 2026-08-25: added -- this dashboard never had a strategy-wise P&L/WR/PF
    # view at all, only per-position rows and raw recent-trades CSV lines.
    # Found while investigating a real ZC stop-loss that was correctly
    # closed+emailed by intraday_monitor.py but showed nowhere else.
    Write-Host "  STRATEGY BREAKDOWN  (all-time realized, Today = today only)" -ForegroundColor Cyan
    Write-Host "  $("-" * 88)" -ForegroundColor DarkGray
    $stats      = @($d.strategy_stats)
    $statsToday = @($d.strategy_stats_today)
    # 2026-08-28: real per-(strategy,symbol) rows, added after the
    # comma-joined "Markets" list (GC, NQ, ZC) still wasn't what was
    # wanted -- "still i can not see the Symbol" -- that list names which
    # markets were traded but gives no per-market stats at all (e.g. no
    # way to tell GC alone drove the whole +3463 while NQ/ZC were flat or
    # losing). symStats below has one real row per strategy+symbol combo.
    $symStats      = @($d.strategy_symbol_stats)
    $symStatsToday = @($d.strategy_symbol_stats_today)
    if ($stats.Count -eq 0) {
        Write-Host "  No closed futures trades yet." -ForegroundColor DarkGray
    } else {
        $todayByStrat = @{}
        foreach ($t in $statsToday) { $todayByStrat[$t.strategy] = $t }
        $symTodayByKey = @{}
        foreach ($t in $symStatsToday) { $symTodayByKey["$($t.strategy)|$($t.symbol)"] = $t }

        Write-Host ("  {0,-10} {1,8} {2,7} {3,8} {4,7} {5,7} {6,14} {7,14}" -f `
            "Strategy","Symbol","Closed","W/L","WR%","PF","All-Time P&L","Today") -ForegroundColor DarkGray
        Write-Host "  $("-" * 88)" -ForegroundColor DarkGray

        foreach ($s in ($stats | Sort-Object -Property total_pnl -Descending)) {
            $wr    = "{0:N1}%" -f $s.win_rate
            $pf    = if ($null -ne $s.profit_factor) { "{0:N2}" -f $s.profit_factor } else { "-" }
            $pnl   = "{0:+0.00;-0.00}" -f [double]$s.total_pnl
            $pnlC  = if ([double]$s.total_pnl -ge 0) { "Green" } else { "Red" }
            $stratC = StratColor $s.strategy
            $todayEntry = $todayByStrat[$s.strategy]
            $todayStr  = if ($todayEntry) { "{0:+0.00;-0.00}" -f [double]$todayEntry.total_pnl } else { "-" }
            $todayC    = if ($todayEntry -and [double]$todayEntry.total_pnl -lt 0) { "Red" } elseif ($todayEntry) { "Green" } else { "DarkGray" }

            # Strategy-level total row (Symbol column blank -- this row is
            # the sum across every symbol below it).
            Write-Host ("  {0,-10} {1,8} " -f $s.strategy.ToUpper(), "") -NoNewline -ForegroundColor $stratC
            $wl = "{0}W/{1}L" -f $s.wins, $s.losses
            Write-Host ("{0,7} {1,8} {2,7} {3,7} " -f $s.trades, $wl, $wr, $pf) -NoNewline -ForegroundColor White
            Write-Host ("{0,14}" -f $pnl) -NoNewline -ForegroundColor $pnlC
            Write-Host ("{0,14}" -f $todayStr) -ForegroundColor $todayC

            # Per-symbol sub-rows, indented, sorted by that symbol's own
            # all-time P&L descending -- the actual "which market did
            # this" answer.
            $symRows = @($symStats | Where-Object { $_.strategy -eq $s.strategy } | Sort-Object -Property total_pnl -Descending)
            foreach ($sym in $symRows) {
                Write-Host ("  {0,-10} {1,8} " -f "", $sym.symbol) -NoNewline -ForegroundColor DarkGray
                if ($sym.unresolved) {
                    # A closed trade whose real P&L was never confirmed
                    # (documented broker-side ambiguity, e.g. futures id 60
                    # CL -- see pnl_tracker.get_strategy_symbol_summary's
                    # docstring). Shown as "P&L UNKNOWN" instead of a
                    # misleading "+0.00", which would read as a real,
                    # known-flat trade.
                    Write-Host ("{0,7} {1,8} {2,7} {3,7} " -f $sym.trades, "-", "-", "-") -NoNewline -ForegroundColor DarkGray
                    Write-Host ("{0,14}" -f "P&L UNKNOWN") -ForegroundColor DarkGray
                    continue
                }
                $symWr   = "{0:N1}%" -f $sym.win_rate
                $symPf   = if ($null -ne $sym.profit_factor) { "{0:N2}" -f $sym.profit_factor } else { "-" }
                $symPnl  = "{0:+0.00;-0.00}" -f [double]$sym.total_pnl
                $symPnlC = if ([double]$sym.total_pnl -ge 0) { "Green" } else { "Red" }
                $symToday = $symTodayByKey["$($sym.strategy)|$($sym.symbol)"]
                $symTodayStr = if ($symToday -and -not $symToday.unresolved) { "{0:+0.00;-0.00}" -f [double]$symToday.total_pnl } elseif ($symToday) { "?" } else { "-" }
                $symTodayC   = if ($symToday -and -not $symToday.unresolved -and [double]$symToday.total_pnl -lt 0) { "Red" } elseif ($symToday -and -not $symToday.unresolved) { "Green" } else { "DarkGray" }
                $symWl = "{0}W/{1}L" -f $sym.wins, $sym.losses

                Write-Host ("{0,7} {1,8} {2,7} {3,7} " -f $sym.trades, $symWl, $symWr, $symPf) -NoNewline -ForegroundColor DarkGray
                Write-Host ("{0,14}" -f $symPnl) -NoNewline -ForegroundColor $symPnlC
                Write-Host ("{0,14}" -f $symTodayStr) -ForegroundColor $symTodayC
            }
        }
    }
    NL

    Write-Host "  LOG HEALTH" -ForegroundColor Cyan
    Write-Host "  $("-" * 44)" -ForegroundColor DarkGray
    $h = $d.health
    if ($h -and $h.log_file) {
        $ranC  = if ($h.ran_today) { "Green" } else { "Yellow" }
        $ranTx = if ($h.ran_today) { "YES" } else { "NO -- not run today yet" }
        Write-Host ("  Log file  :  {0}" -f $h.log_file) -ForegroundColor DarkGray
        Write-Host ("  Ran today :  {0}" -f $ranTx) -ForegroundColor $ranC
        $age  = Fmt-MinAgo $h.log_age_minutes
        $ageC = if ($h.log_age_minutes -gt 120) { "Yellow" } else { "Green" }
        Write-Host ("  Last write:  {0}" -f $age) -ForegroundColor $ageC
        Write-Host ("  Log dir   :  {0}" -f $LogDir) -ForegroundColor DarkGray
    } else {
        Write-Host "  No log file yet -- runner has not run today." -ForegroundColor DarkGray
    }
    NL

    Write-Host "  $line" -ForegroundColor DarkGray
    Write-Host ("  As of: {0}  |  CSV log: {1}" -f $d.as_of, $TradeLog) -ForegroundColor DarkGray
    NL
}

function Run-Scan {
    NL
    Write-Host "  FUTURES MARKET SCAN -- 6 panels" -ForegroundColor Cyan
    Write-Host "  $("-" * 60)" -ForegroundColor DarkGray
    NL
    & $Python -X utf8 $Runner --scan
}

function Run-Trade([bool]$dryRun) {
    $modeLabel = if ($dryRun) { "DRY-RUN" } else { "LIVE -- REAL ORDERS" }
    $modeColor = if ($dryRun) { "Yellow" } else { "Red" }
    NL
    Write-Host ("  FUTURES RUNNER -- {0}" -f $modeLabel) -ForegroundColor $modeColor
    Write-Host "  $("-" * 60)" -ForegroundColor DarkGray
    NL

    if ($dryRun) {
        Write-Host "  Dry run -- no real orders placed." -ForegroundColor Yellow
        & $Python -X utf8 $Runner
    } else {
        Write-Host "  Placing REAL orders on Saxo SIM account." -ForegroundColor Red
        & $Python -X utf8 $Runner --live
    }

    NL
    Write-Host "  Done. Refreshing dashboard..." -ForegroundColor DarkGray
    Start-Sleep -Seconds 2
    Draw-Dashboard
}

if ($Scan) {
    Run-Scan
} elseif ($Trade) {
    Draw-Dashboard
    Write-Host "  Press ENTER to place LIVE orders, or Ctrl+C to abort..." -ForegroundColor Red
    $null = Read-Host
    Run-Trade -dryRun $false
} elseif ($Dry) {
    Run-Trade -dryRun $true
} elseif ($Watch) {
    while ($true) {
        Clear-Host
        Draw-Dashboard
        Write-Host "  Next refresh in 60s...  (Ctrl+C to exit)" -ForegroundColor DarkGray
        Start-Sleep -Seconds 60
    }
} else {
    Draw-Dashboard
}
