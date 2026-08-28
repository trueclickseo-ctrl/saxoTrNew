"""
Regression tests -- 2026-08-28 futures dashboard per-symbol breakdown.

Explicit user complaint: "in Futures Dashboard I can see the strategy
name but no the symbol which is sold or bought, there is missing
information" -- pasted the STRATEGY BREAKDOWN table (Strategy/Closed/
W-L/WR%/PF/All-Time P&L/Today) with no indication of which of the 13
futures markets each strategy's trades were actually on.

First attempt (same day): added get_strategy_summary()'s "symbols"
field + a comma-joined "Markets" column ("DONCHIAN ... GC, NQ, ZC").
User reported "still i can not see the Symbol" even after that --  a
comma list names which markets were traded but gives no per-market
stats, so it wasn't actually answering the question. Superseded by
pnl_tracker.get_strategy_symbol_summary() (real per-(strategy,symbol)
rows) + indented per-symbol sub-rows under each strategy in
dashboard_futures.ps1's STRATEGY BREAKDOWN, each with its own
Closed/W-L/WR%/PF/All-Time P&L/Today. The old get_strategy_summary()
"symbols" field / "Markets" column tests below still apply -- that
field wasn't removed, just no longer the dashboard's primary answer to
"which symbol."
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
section("2. pnl_tracker.get_strategy_symbol_summary() -- real per-symbol rows")
# ═══════════════════════════════════════════════════════════════════════

def test_get_strategy_symbol_summary_returns_real_rows():
    import pnl_tracker
    rows = pnl_tracker.get_strategy_symbol_summary("futures")
    assert rows, "expected at least one (strategy, symbol) row for futures"
    donchian_syms = {r["symbol"] for r in rows if r["strategy"] == "donchian"}
    assert donchian_syms == {"GC", "NQ", "ZC"}, f"expected donchian's real symbols, got {donchian_syms}"
_run("pnl_tracker.get_strategy_symbol_summary('futures') returns real per-symbol rows",
     test_get_strategy_symbol_summary_returns_real_rows)


def test_unresolved_symbol_row_is_flagged_not_fabricated_zero():
    # futures id 60 (macd/CL) has realized_pnl=NULL -- a documented,
    # deliberately-unresolved broker ambiguity. Its (strategy, symbol) row
    # must be flagged 'unresolved': True with total_pnl=None, not silently
    # folded in as a real "+0.00" (which pnl_tracker's SQL SUM()/CASE-WHEN
    # would otherwise produce, since NULL contributes 0 to a SUM).
    import pnl_tracker
    rows = {(r["strategy"], r["symbol"]): r for r in pnl_tracker.get_strategy_symbol_summary("futures")}
    cl_row = rows.get(("macd", "CL"))
    assert cl_row is not None, "expected a (macd, CL) row"
    assert cl_row["unresolved"] is True, f"expected CL's row to be flagged unresolved, got {cl_row}"
    assert cl_row["total_pnl"] is None, f"expected CL's total_pnl to be None (unknown), got {cl_row['total_pnl']}"
    zb_row = rows.get(("macd", "ZB"))
    assert zb_row is not None and zb_row["unresolved"] is False, (
        "ZB has a real (documented) $0.0 P&L -- must NOT be flagged unresolved"
    )
_run("get_strategy_symbol_summary() flags a NULL-P&L row as 'unresolved', doesn't fabricate a '+0.00'",
     test_unresolved_symbol_row_is_flagged_not_fabricated_zero)


# ═══════════════════════════════════════════════════════════════════════
section("3. Blackbox -- dashboard_futures.ps1 shows a real Symbol breakdown")
# ═══════════════════════════════════════════════════════════════════════

def test_powershell_dashboard_shows_symbol_breakdown():
    proc = subprocess.run(
        ["powershell", "-File", os.path.join(BASE_DIR, "dashboard_futures.ps1"), "-Once"],
        cwd=BASE_DIR, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    out = proc.stdout
    assert "Symbol" in out, "expected a 'Symbol' column header in STRATEGY BREAKDOWN"
    assert "GC" in out and "NQ" in out and "ZC" in out, (
        "expected donchian's real per-symbol rows (GC/NQ/ZC) to appear in STRATEGY BREAKDOWN"
    )
    assert "P&L UNKNOWN" in out, (
        "expected macd/CL's row to show 'P&L UNKNOWN', not a fabricated '+0.00'"
    )
_run("dashboard_futures.ps1 -Once shows real per-symbol rows under each strategy, with CL correctly marked P&L UNKNOWN",
     test_powershell_dashboard_shows_symbol_breakdown)


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
