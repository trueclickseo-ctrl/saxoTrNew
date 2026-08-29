"""
Regression tests -- 2026-08-27 real currency-exposure cap for LIVE accounts.

Real incident this closes: LIVE-SEK's donchian strategy opened AUDCHF Buy
and (what was then mislabeled) "CADCHF" Sell within hours of each other.
Decomposed into currency exposure (not ticker names), both are the exact
same bet: long AUD, short CHF. MAX_CURRENCY_EXPOSURE has been unlimited
(999) since 2026-08-21 at the user's explicit request, for SIM signal-
testing breadth -- but its own code comment already said "reconsider
before trading live capital." This is that reconsideration: SIM stays
unlimited, LIVE and LIVE_EUR cap net exposure per currency (initially 1;
raised to 5 on 2026-08-29 -- see forex/runner.py comment).
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
    # 2026-08-29: raised 1 -> 5 (LIVE_EUR was rejecting nearly every RSI
    # signal on the single-USD-slot limit -- see runner.py comment).
    import forex.runner as r
    r.set_account_env("live")
    assert r._max_currency_exposure() == 5
    r.set_account_env("live_eur")
    assert r._max_currency_exposure() == 5
_run("forex/runner: live and live_eur both get the real cap (5)",
     test_live_gets_real_cap)


def test_live_cap_boundary_at_five():
    """With the cap at 5: a 6th position pushing the same currency the same
    direction is blocked; the 5th is still allowed."""
    import forex.runner as r
    r.set_account_env("live")
    # four existing USD-short positions (long XXXUSD)
    four_usd_short = {f"rsi:AAA{i}": {"direction": "Buy"} for i in range(4)}
    exposure = {"USD": -4}
    assert r._currency_ok("EURUSD", "Buy", exposure) is True, (
        "5th USD-short position must still fit under a cap of 5")
    exposure = {"USD": -5}
    assert r._currency_ok("EURUSD", "Buy", exposure) is False, (
        "6th USD-short position must be blocked at a cap of 5")
_run("forex/runner: live currency-exposure cap blocks the 6th same-side "
     "position, allows the 5th",
     test_live_cap_boundary_at_five)


def test_audchf_chfaud_doubling_now_allowed_on_live_under_cap_5():
    """The original incident (AUDCHF Buy + CHFAUD Sell = same long-AUD/
    short-CHF bet) reaches net exposure 2, which is now UNDER the cap of 5,
    so it is deliberately allowed again. Documented trade-off of raising
    the cap -- kept as an explicit assertion so the behavior change is
    visible, not silent."""
    import forex.runner as r
    r.set_account_env("live")
    existing_exposure = r._currency_exposure({"donchian:AUDCHF":
                                               {"direction": "Buy"}})
    assert existing_exposure == {"AUD": 1, "CHF": -1}
    ok = r._currency_ok("CHFAUD", "Sell", existing_exposure)
    assert ok is True, (
        "net AUD/CHF exposure of 2 is under the cap of 5 -- allowed by "
        "design after the 2026-08-29 change")
_run("forex/runner: AUDCHF+CHFAUD doubling is now allowed on live under "
     "cap 5 (documented trade-off of the raise)",
     test_audchf_chfaud_doubling_now_allowed_on_live_under_cap_5)


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
