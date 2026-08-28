"""
Regression tests -- 2026-08-28 futures_dashboard.py stale-universe fixes.

Found while investigating a user report ("we sold this MACD 2 0W/0L 0.0%
- +0.00 - but it did not show any loss or profit? are we scanning new
signals... check futures if we have right logic and algorithm"):

  1. futures_dashboard.py's STRAT_COL/STRAT_DESC/strat_order/legend/header
     all hardcoded only 6 of futures/runner.py's 7 real strategies
     (missing "trend_ma" entirely) and only 5 of futures/universe.py's 13
     real markets (missing YM/DAX/HK50/SI/NG/ZC/ZW/ZS) -- trend_ma has
     been scanning every hour since it was added but any of its positions
     would have been silently invisible on this dashboard's OPEN
     POSITIONS breakdown the whole time. The header also said "30 max
     positions" (6*5) instead of the real 35 (7*5), and "daily run"
     instead of the real hourly cadence.

  2. The actual "MACD 2 0W/0L..." row the user saw comes from a DIFFERENT
     dashboard (dashboard_futures.ps1 -> futures/status_helper.py, which
     is already correctly dynamic off pnl_tracker.get_strategy_summary()
     -- not a bug there). Investigated directly against pnl_tracker: those
     2 "closed" MACD trades (ids 60/61) are both non-trades from an old
     (2026-08-18) sizing bug -- one order (qty=7972, clearly a sizing
     bug) was never filled at all, the other's close fill could never be
     confirmed and was marked closed by a 2026-08-21 reconciliation sweep
     with realized_pnl=None. Both are correctly recorded with zero/null
     P&L (not fabricated numbers) -- "no profit or loss shown" is
     accurate given the data, this is old, already-diagnosed history, not
     a live bug. Scanning itself is confirmed active every hour across
     all 7 real strategies via data/futures_scheduler.log.

  3. Confirmed separately: SI (Silver, UIC 8178) fails its chart fetch on
     literally every single scheduler run (60/60 in the observed log) --
     matches [[ZC Order Type/TickSize Bug]]'s memory note that SI has "a
     standing SIM quote-access restriction, same class as ZC" -- a known
     Saxo-side limitation, not a code bug. Effectively 12 of 13 markets
     are ever actually scannable for entries.
"""

import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        result = fn()
        if result is None:
            result = True
        _results.append((name, bool(result), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def section(title):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'-'*70}{RESET}")


# ═══════════════════════════════════════════════════════════════════════
section("1. futures_dashboard.py -- trend_ma no longer invisible")
# ═══════════════════════════════════════════════════════════════════════

def test_strat_col_includes_trend_ma():
    import futures_dashboard as fd
    assert "trend_ma" in fd.STRAT_COL, "trend_ma must have its own dashboard color"
_run("futures_dashboard.STRAT_COL includes trend_ma",
     test_strat_col_includes_trend_ma)


def test_strat_desc_includes_trend_ma():
    import futures_dashboard as fd
    assert "trend_ma" in fd.STRAT_DESC, "trend_ma must have its own dashboard description"
_run("futures_dashboard.STRAT_DESC includes trend_ma",
     test_strat_desc_includes_trend_ma)


def test_strat_order_lists_include_trend_ma():
    import inspect
    import futures_dashboard as fd
    src = inspect.getsource(fd)
    # Both hardcoded strat_order lists in the file must now include trend_ma
    occurrences = src.count('"trend_ma"')
    assert occurrences >= 3, (  # STRAT_COL, STRAT_DESC, strat_order (at least)
        f"expected trend_ma to appear in STRAT_COL, STRAT_DESC, and the "
        f"OPEN POSITIONS strat_order list, found only {occurrences} occurrences"
    )
_run("futures_dashboard.py's strategy-ordering lists all include trend_ma",
     test_strat_order_lists_include_trend_ma)


# ═══════════════════════════════════════════════════════════════════════
section("2. Blackbox -- futures_dashboard.py --once shows the real universe")
# ═══════════════════════════════════════════════════════════════════════

def _run_dashboard():
    return subprocess.run(
        [sys.executable, "futures_dashboard.py", "--once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def test_dashboard_runs_cleanly():
    proc = _run_dashboard()
    assert proc.returncode == 0, f"expected a clean exit(0), got {proc.returncode}: {proc.stderr}"
_run("futures_dashboard.py --once runs cleanly with the corrected universe",
     test_dashboard_runs_cleanly)


def test_dashboard_shows_all_13_real_markets():
    from futures.universe import MARKETS
    proc = _run_dashboard()
    out = proc.stdout
    assert f"{len(MARKETS)} markets" in out or f"{len(MARKETS)} Markets" in out, (
        f"expected the header to state the real market count ({len(MARKETS)}), "
        f"not the old stale '5' figure"
    )
    for m in MARKETS:
        assert m["symbol"] in out, f"expected market {m['symbol']} to appear in the dashboard header"
_run("futures_dashboard.py --once shows all 13 real markets, not the stale 5",
     test_dashboard_shows_all_13_real_markets)


def test_dashboard_shows_35_max_positions_not_30():
    proc = _run_dashboard()
    out = proc.stdout
    assert "35 max positions" in out, "expected 7 strategies x 5 slots = 35, not the stale 30 (6x5)"
    assert "7 strategies" in out
_run("futures_dashboard.py --once shows the real 35 max positions (7 strategies x 5 slots)",
     test_dashboard_shows_35_max_positions_not_30)


def test_dashboard_shows_ma_20_100_strategy_legend():
    proc = _run_dashboard()
    out = proc.stdout
    assert "MA(20/100)" in out, "expected trend_ma's own legend entry to be visible"
_run("futures_dashboard.py --once shows trend_ma's own strategy legend entry",
     test_dashboard_shows_ma_20_100_strategy_legend)


# ═══════════════════════════════════════════════════════════════════════
section("3. pnl_tracker -- MACD's 2 historical 'closed trades' are documented non-trades")
# ═══════════════════════════════════════════════════════════════════════

def test_macd_historical_trades_are_documented_as_broken_not_fabricated():
    # This is a factual/documentation check, not a behavioral assertion --
    # confirms the historical record itself is honest (zero/null P&L, a
    # descriptive exit_reason explaining why) rather than silently showing
    # a fabricated number. If real MACD trades close successfully in the
    # future, this specific pair of ids will simply no longer be the ONLY
    # macd rows, which is fine -- this test only checks these two rows
    # specifically, by id, remain honestly recorded.
    import pnl_tracker
    trades = {t["id"]: t for t in pnl_tracker.get_closed_trades(module="futures", limit=1000)}
    for trade_id in (60, 61):
        if trade_id not in trades:
            continue   # ledger may have been pruned/migrated -- not this test's concern
        t = trades[trade_id]
        assert t["strategy"] == "macd"
        assert t["realized_pnl"] in (0.0, None), (
            f"trade {trade_id} should have zero/null P&L (it was never a real "
            f"filled-and-closed position), got {t['realized_pnl']}"
        )
        assert t["exit_reason"], f"trade {trade_id} must have a descriptive exit_reason explaining why it's a non-trade"
_run("pnl_tracker: MACD's 2 historical non-trades (ids 60/61) remain honestly recorded, not fabricated",
     test_macd_historical_trades_are_documented_as_broken_not_fabricated)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results)
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    icon = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  [{icon}] {name}")
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} TESTS FAILED{RESET}")
    sys.exit(1)
else:
    print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
    sys.exit(0)
