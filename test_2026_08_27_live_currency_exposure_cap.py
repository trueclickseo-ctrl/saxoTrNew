"""
Regression tests -- 2026-08-27 real currency-exposure cap for LIVE accounts.

Real incident this closes: LIVE-SEK's donchian strategy opened AUDCHF Buy
and (what was then mislabeled) "CADCHF" Sell within hours of each other.
Decomposed into currency exposure (not ticker names), both are the exact
same bet: long AUD, short CHF. MAX_CURRENCY_EXPOSURE has been unlimited
(999) since 2026-08-21 at the user's explicit request, for SIM signal-
testing breadth -- but its own code comment already said "reconsider
before trading live capital." This is that reconsideration: SIM stays
unlimited, LIVE and LIVE_EUR now cap net exposure per currency at 1.
"""

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
        result = fn()
        if result is None:
            result = True
        _results.append((name, bool(result), None))
    except Exception as e:
        _results.append((name, False, f"{type(e).__name__}: {e}"))


def test_sim_stays_unlimited():
    import forex.runner as r
    r.set_account_env("sim")
    assert r._max_currency_exposure() == 999, (
        "SIM must stay unlimited -- explicit 2026-08-21 user request for "
        "full signal-testing breadth, not touched by this change")
_run("forex/runner: SIM's currency exposure limit stays unlimited (999)",
     test_sim_stays_unlimited)


def test_live_gets_real_cap():
    import forex.runner as r
    r.set_account_env("live")
    assert r._max_currency_exposure() == 1
    r.set_account_env("live_eur")
    assert r._max_currency_exposure() == 1
_run("forex/runner: live and live_eur both get the real cap (1)",
     test_live_gets_real_cap)


def test_audchf_chfaud_scenario_now_blocked_on_live():
    """The actual incident, replayed: holding AUDCHF Buy, then trying to
    open CHFAUD Sell (the same long-AUD/short-CHF bet) must now be blocked
    on a live account."""
    import forex.runner as r
    r.set_account_env("live")
    existing_exposure = r._currency_exposure({"donchian:AUDCHF":
                                               {"direction": "Buy"}})
    assert existing_exposure == {"AUD": 1, "CHF": -1}
    ok = r._currency_ok("CHFAUD", "Sell", existing_exposure)
    assert ok is False, (
        "AUDCHF Buy already held -> CHFAUD Sell (same long-AUD/short-CHF "
        "bet) must be blocked on a live account with the cap active")
_run("forex/runner: the real AUDCHF+CHFAUD doubling scenario is now "
     "blocked on live (replayed against _currency_ok directly)",
     test_audchf_chfaud_scenario_now_blocked_on_live)


def test_same_scenario_still_allowed_on_sim():
    import forex.runner as r
    r.set_account_env("sim")
    existing_exposure = r._currency_exposure({"donchian:AUDCHF":
                                               {"direction": "Buy"}})
    ok = r._currency_ok("CHFAUD", "Sell", existing_exposure)
    assert ok is True, (
        "SIM must still allow this -- unlimited exposure is deliberate "
        "there, only LIVE changed")
_run("forex/runner: the same scenario is still allowed on SIM (unchanged "
     "behavior, per the 2026-08-21 standing instruction)",
     test_same_scenario_still_allowed_on_sim)


def test_entry_loop_uses_dynamic_limit_not_bare_constant():
    import inspect
    import forex.runner as r
    src = inspect.getsource(r._run_entries)
    assert "_currency_ok(sym, direction, exposure)" in src
    # the log line must reflect whichever limit actually applied
    assert "_max_currency_exposure()" in src
_run("forex/runner: _run_entries()'s skip-reason log reflects the real "
     "applied limit, not always the SIM constant",
     test_entry_loop_uses_dynamic_limit_not_bare_constant)


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
