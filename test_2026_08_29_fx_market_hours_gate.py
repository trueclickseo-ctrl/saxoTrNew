"""
Regression tests -- 2026-08-29 FX weekend market-hours gate.

Problem it closes: a real-money RSI/BB signal computed on stale Friday data
was being sent as a Market order that rested on the closed weekend market
and would fill at Monday's open, at an unrelated price, with no re-check of
the setup. Now: live/live_eur place NO new entries while FX is closed
(~Fri 22:00 UTC -> ~Sun 22:00 UTC). Exits/stops are unaffected. SIM is
unaffected (scans/trades all 7 days by design). Gap strategies are exempt
(own session windows; gap_weekend trades the Sunday reopen).
"""

import inspect
import os
import sys
from datetime import datetime, timezone

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


def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_market_open_closed_boundaries():
    import forex.runner as r
    # 2026-08-28 is a Friday, 2026-08-29 Saturday, 2026-08-30 Sunday, 2026-08-31 Monday
    assert r._fx_market_open(_utc(2026, 8, 28, 21, 0)) is True,  "Fri 21:00 UTC still open"
    assert r._fx_market_open(_utc(2026, 8, 28, 22, 0)) is False, "Fri 22:00 UTC closed"
    assert r._fx_market_open(_utc(2026, 8, 29, 3, 0))  is False, "Saturday always closed"
    assert r._fx_market_open(_utc(2026, 8, 29, 23, 0)) is False, "Saturday always closed"
    assert r._fx_market_open(_utc(2026, 8, 30, 21, 59)) is False, "Sun 21:59 UTC still closed"
    assert r._fx_market_open(_utc(2026, 8, 30, 22, 0))  is True,  "Sun 22:00 UTC reopen"
    assert r._fx_market_open(_utc(2026, 8, 31, 12, 0))  is True,  "Monday open"
    assert r._fx_market_open(_utc(2026, 9, 2, 3, 0))    is True,  "Wednesday open"
_run("forex/runner: _fx_market_open weekend boundaries (Fri 22:00 - Sun 22:00 UTC closed)",
     test_market_open_closed_boundaries)


def test_gate_is_live_only_and_exempts_gap_strats():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "_fx_market_open()" in src
    # the block flag definition (a 3-condition parenthesised assignment)
    start = src.index("_weekend_entry_block = (")
    flag = src[start:start + 300]
    assert 'ACCOUNT_ENV in ("live", "live_eur")' in flag, "must be live-only"
    assert "strat_name not in _GAP_STRATS" in flag, "gap strategies must be exempt"
    assert "not _fx_market_open()" in flag
    # enforcement: when the flag is set, return 0 without entering
    enforce = src[src.index("if _weekend_entry_block:"):]
    enforce = enforce[:enforce.index("return 0") + len("return 0")]
    assert "return 0" in enforce
_run("forex/runner: weekend entry gate is live/live_eur only and exempts gap strategies",
     test_gate_is_live_only_and_exempts_gap_strats)


def test_weekend_still_generates_and_emails_signals():
    """The gate is a flag, not an early return before signal generation --
    a weekend signal must still be produced and emailed, just not entered."""
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    # signal generation happens before the weekend enforcement
    gen_at   = src.index("generate_signals")
    block_at = src.index("if _weekend_entry_block:")
    assert gen_at < block_at, "signals must be generated before the weekend block"
    wk = src[block_at:src.index("return 0", block_at)]
    assert "send_signals_detected" in wk and "market_closed=True" in wk
_run("forex/runner: weekend scan still generates + emails signals (market_closed=True)",
     test_weekend_still_generates_and_emails_signals)


def test_live_signals_emailed_on_normal_runs_too():
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    tail = src[src.rindex("return entries") - 2200:]
    assert "send_signals_detected" in tail and "entered=entered_syms" in tail
    assert 'ACCOUNT_ENV in ("live", "live_eur")' in tail
_run("forex/runner: LIVE signals are emailed on normal (open-market) runs too",
     test_live_signals_emailed_on_normal_runs_too)


def test_exits_path_has_no_market_hours_gate():
    import forex.runner as r
    # run_exits_only must NOT gate on _fx_market_open -- stops/exits always run
    src = inspect.getsource(r.run_exits_only)
    assert "_fx_market_open" not in src, (
        "exits-only must never be blocked by market hours")
_run("forex/runner: run_exits_only is not gated by market hours (stops always run)",
     test_exits_path_has_no_market_hours_gate)


def test_scanning_not_narrowed_midweek():
    """The gate only blocks entry PLACEMENT on the weekend; it must not
    touch which pairs get scanned. run_daily's pair selection has no
    _fx_market_open reference."""
    import forex.runner as r
    src = inspect.getsource(r.run_daily)
    assert "_fx_market_open" not in src, (
        "run_daily's scan/pair-selection must not depend on market hours")
_run("forex/runner: run_daily still scans the full pair set (gate is entry-only, in _run_entries)",
     test_scanning_not_narrowed_midweek)


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
