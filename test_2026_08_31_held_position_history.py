"""
Regression test -- 2026-08-31 exit management for held positions on pairs
NOT in the account's current scanned universe.

Found live: `rsi:GBPPLN` on the LIVE EUR account was opened 2026-08-26 as an
exotic pair, before that account was narrowed to CORE_SYMBOLS only. From
then on `market_data.get("GBPPLN")` returned None every run -- market_data
is only ever built for `active_pairs`. With df=None, EVERY exit path
silently no-ops: the generic trailing block, _apply_breakeven_stop,
_apply_profit_ladder_stop, and strategy_rsi.should_exit (which
early-returns on df=None, so even the 12-day time stop never fires). The
position ran +30 -> -24 PLN managed only by its entry-day broker stop/TP.

Fix: `_add_held_position_history(market_data, positions)` -- called right
after the active-pairs fetch in both run_daily and run_exits_only -- pulls
daily history for any held symbol missing from market_data, so a legacy
position on a since-dropped pair is fully exit-managed again.
"""

import inspect
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

GREEN, RED, YELLOW, CYAN, RESET, BOLD = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[0m", "\033[1m"
)
_results = []


def _run(name, fn):
    try:
        fn()
        _results.append((name, True, None))
    except Exception as e:
        import traceback
        _results.append((name, False, f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def section(t):
    print(f"\n{BOLD}{CYAN}{'-'*70}{RESET}\n{BOLD}{CYAN}  {t}{RESET}\n{BOLD}{CYAN}{'-'*70}{RESET}")


import forex.runner as r


# ═══════════════════════════════════════════════════════════════════════
section("1. _add_held_position_history fetches missing held-pair history")
# ═══════════════════════════════════════════════════════════════════════

def test_fetches_only_missing_held_symbols():
    calls = []
    orig = r._fetch_history
    r._fetch_history = lambda uic, *a, **k: (calls.append(uic) or "DF")
    try:
        # EURUSD already in market_data (scanned) -> must NOT be refetched.
        # GBPPLN held but absent -> must be fetched via its universe uic.
        md = {"EURUSD": "existing"}
        positions = {"rsi:EURUSD": {}, "rsi:GBPPLN": {}, "donchian:AUDCHF": {}}
        r._add_held_position_history(md, positions)
        gbppln_uic = r._PAIRS_BY_SYMBOL["GBPPLN"]["uic"]
        audchf_uic = r._PAIRS_BY_SYMBOL["AUDCHF"]["uic"]
        assert md["EURUSD"] == "existing", "a symbol already present must be left untouched"
        assert md.get("GBPPLN") == "DF", "held-but-missing GBPPLN must be fetched"
        assert md.get("AUDCHF") == "DF", "held-but-missing AUDCHF must be fetched"
        assert set(calls) == {gbppln_uic, audchf_uic}
        assert gbppln_uic not in (r._PAIRS_BY_SYMBOL["EURUSD"]["uic"],), "sanity"
    finally:
        r._fetch_history = orig
_run("only held symbols missing from market_data are fetched, by their real universe uic",
     test_fetches_only_missing_held_symbols)


def test_unknown_symbol_is_skipped_not_crashed():
    orig = r._fetch_history
    r._fetch_history = lambda *a, **k: "DF"
    try:
        md = {}
        r._add_held_position_history(md, {"rsi:NOTAPAIR": {}})
        assert "NOTAPAIR" not in md, "a symbol not in the universe at all is skipped, no crash"
    finally:
        r._fetch_history = orig
_run("a held position on a symbol not in the universe is skipped without crashing",
     test_unknown_symbol_is_skipped_not_crashed)


def test_no_held_positions_is_a_noop():
    orig = r._fetch_history
    hits = []
    r._fetch_history = lambda uic, *a, **k: hits.append(uic)
    try:
        md = {"EURUSD": "x"}
        r._add_held_position_history(md, {})
        assert hits == [] and md == {"EURUSD": "x"}
    finally:
        r._fetch_history = orig
_run("no open positions -> no fetches, market_data unchanged", test_no_held_positions_is_a_noop)


# ═══════════════════════════════════════════════════════════════════════
section("2. wired into both run entry points, before the exit loop")
# ═══════════════════════════════════════════════════════════════════════

def test_called_in_run_daily_and_run_exits_only():
    for fn in (r.run_daily, r.run_exits_only):
        src = inspect.getsource(fn)
        assert "_add_held_position_history(market_data, positions)" in src, (
            f"{fn.__name__} must top up market_data with held-position history"
        )
        # must come after the active-pairs fetch and before the strategy loop
        add_at = src.index("_add_held_position_history(market_data, positions)")
        loop_at = src.index("for strat_name in active_strategies")
        assert add_at < loop_at, f"{fn.__name__}: the top-up must run before the exit loop"
_run("_add_held_position_history is called in run_daily AND run_exits_only, before the exit loop",
     test_called_in_run_daily_and_run_exits_only)


print(f"\n{BOLD}{'='*70}{RESET}")
passed = sum(1 for _, ok, _ in _results if ok)
failed = [(n, e) for n, ok, e in _results if not ok]
for name, ok, err in _results:
    print(f"  [{GREEN}PASS{RESET}]" if ok else f"  [{RED}FAIL{RESET}]", name)
    if err:
        print(f"         {YELLOW}{err}{RESET}")
print(f"{BOLD}{'='*70}{RESET}")
if failed:
    print(f"{RED}{BOLD}  {len(failed)} / {len(_results)} FAILED{RESET}")
    sys.exit(1)
print(f"{GREEN}{BOLD}  ALL {len(_results)} TESTS PASSED{RESET}")
sys.exit(0)
