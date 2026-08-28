"""
Regression tests -- 2026-08-28 futures dashboard "Markets" column.

Explicit user complaint: "in Futures Dashboard I can see the strategy
name but no the symbol which is sold or bought, there is missing
information" -- pasted the STRATEGY BREAKDOWN table (Strategy/Closed/
W-L/WR%/PF/All-Time P&L/Today) with no indication of which of the 13
futures markets each strategy's trades were actually on.

pnl_tracker.get_strategy_summary() (all-time) previously had no
"symbols" field at all -- only its _since() sibling (used for daily
digests) computed the DISTINCT-symbol breakdown. Added the same query
to the all-time function and a new "Markets" column to
dashboard_futures.ps1's STRATEGY BREAKDOWN table.
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
section("1. pnl_tracker.get_strategy_summary() -- new 'symbols' field")
# ═══════════════════════════════════════════════════════════════════════

def test_get_strategy_summary_returns_symbols_field():
    import pnl_tracker
    rows = pnl_tracker.get_strategy_summary("futures")
    assert rows, "expected at least one strategy row for futures (donchian/macd historically)"
    for r in rows:
        assert "symbols" in r, f"strategy {r.get('strategy')} row is missing the new 'symbols' field"
        assert isinstance(r["symbols"], list), "'symbols' must be a list"
_run("pnl_tracker.get_strategy_summary('futures') rows all include a 'symbols' list",
     test_get_strategy_summary_returns_symbols_field)


def test_donchian_symbols_are_real_markets():
    import pnl_tracker
    rows = {r["strategy"]: r for r in pnl_tracker.get_strategy_summary("futures")}
    assert "donchian" in rows, "expected a donchian row (4 historically-closed futures trades)"
    syms = rows["donchian"]["symbols"]
    assert len(syms) > 0, "donchian closed trades exist but its symbols list is empty"
    assert all(isinstance(s, str) and s for s in syms), f"expected real market tickers, got {syms}"
_run("pnl_tracker.get_strategy_summary('futures')['donchian']['symbols'] lists real markets, not empty",
     test_donchian_symbols_are_real_markets)


def test_symbols_field_does_not_break_forex_callers():
    # forex_dashboard.py reads get_strategy_summary()'s dict by key, never
    # asserts an exact key set -- adding 'symbols' must be additive only.
    import pnl_tracker
    rows = pnl_tracker.get_strategy_summary("forex")
    for r in rows:
        for key in ("strategy", "trades", "wins", "losses", "win_rate", "total_pnl", "profit_factor"):
            assert key in r, f"pre-existing key {key!r} missing -- symbols addition must not remove fields"
_run("pnl_tracker.get_strategy_summary('forex') still has every pre-existing key (additive change only)",
     test_symbols_field_does_not_break_forex_callers)


# ═══════════════════════════════════════════════════════════════════════
section("2. Blackbox -- dashboard_futures.ps1 shows a Markets column")
# ═══════════════════════════════════════════════════════════════════════

def test_powershell_dashboard_shows_markets_column():
    proc = subprocess.run(
        ["powershell", "-File", os.path.join(BASE_DIR, "dashboard_futures.ps1"), "-Once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    out = proc.stdout
    assert "Markets" in out, "expected a 'Markets' column header in STRATEGY BREAKDOWN"
    assert "GC" in out or "NQ" in out or "ZC" in out, (
        "expected at least one real market ticker to appear in the STRATEGY BREAKDOWN rows"
    )
_run("dashboard_futures.ps1 -Once shows a 'Markets' column with real ticker(s) in STRATEGY BREAKDOWN",
     test_powershell_dashboard_shows_markets_column)


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
